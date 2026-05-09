"""Pre-flight semantic smoke tests for the KBUtilLib composition refactor.

These tests lock the behavioral contract of the current multi-inheritance
codebase. Task 2 of the composition refactor must keep all of them passing
to prove that the refactor preserves behavior.

Reference: agent-io/prds/kbutillib-composition-refactor/fullprompt.md §7
"""

import subprocess

import pytest


# ── 1-3. MSBiochemUtils tests ────────────────────────────────────────────


class TestMSBiochemUtils:
    """Tests 1-3 from PRD §7.2 — MSBiochemUtils invariants."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("modelseedpy", reason="modelseedpy required")

    @pytest.fixture
    def biochem(self):
        from kbutillib.ms_biochem_utils import MSBiochemUtils

        return MSBiochemUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )

    def test_get_compound_by_id_water(self, biochem):
        """get_compound_by_id('cpd00001') returns compound with name containing H2O or Water."""
        cpd = biochem.get_compound_by_id("cpd00001")
        assert cpd is not None, "cpd00001 (water) must be present in ModelSEED DB"
        name = (getattr(cpd, "name", "") or "").lower()
        assert "h2o" in name or "water" in name, f"Expected water, got name={name}"

    def test_search_compounds_glucose(self, biochem):
        """search_compounds('glucose') returns results including cpd00027."""
        results = biochem.search_compounds(query_identifiers=["glucose"])
        assert len(results) > 0, "Search for 'glucose' should return results"
        result_ids = {r.get("id") if isinstance(r, dict) else getattr(r, "id", None) for r in results}
        assert "cpd00027" in result_ids, (
            f"cpd00027 (glucose) should be in search results, got {result_ids}"
        )

    def test_reaction_directionality_from_bounds_reversible(self, biochem, mini_model):
        """reaction_directionality_from_bounds(reversible_rxn) returns 'reversible'."""
        # R1 in mini_model is reversible (lower_bound=-1000, upper_bound=1000)
        rxn = mini_model.reactions.get_by_id("R1")
        result = biochem.reaction_directionality_from_bounds(rxn)
        # Actual method returns the string 'reversible' (not '=' — the PRD
        # table's '=' refers to the post-refactor direction_conversion value).
        assert result == "reversible", f"Expected 'reversible', got '{result}'"


# ── 4-6. MSFBAUtils tests ───────────────────────────────────────────────


class TestMSFBAUtils:
    """Tests 4-6 from PRD §7.2 — MSFBAUtils invariants (AP3 carve-outs)."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("modelseedpy", reason="modelseedpy required")
        pytest.importorskip("cobrakbase", reason="cobrakbase required")

    @pytest.fixture
    def fba(self):
        from kbutillib.ms_fba_utils import MSFBAUtils

        return MSFBAUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )

    def test_fba_mini_model_produces_growth(self, fba, mini_model):
        """FBA on mini_model with bio1 objective produces flux > 0."""
        sol = fba.run_fba(mini_model, objective="MAX{bio1}")
        assert sol.objective_value > 0, (
            f"FBA objective should be > 0, got {sol.objective_value}"
        )

    def test_run_fva_produces_ranges(self, fba, mini_model):
        """run_fva on mini_model produces N reactions with nonzero ranges."""
        results = fba.run_fva(mini_model)
        assert isinstance(results, dict), "run_fva should return a dict"
        nonzero = sum(
            1 for v in results.values()
            if abs(v.get("MAX", 0)) > 1e-9 or abs(v.get("MIN", 0)) > 1e-9
        )
        assert nonzero > 0, "FVA should have at least one reaction with nonzero range"

    def test_analyzed_reaction_objective_coupling(self, fba, mini_model):
        """analyzed_reaction_objective_coupling categorizes reactions."""
        sol = fba.run_fba(mini_model, objective="MAX{bio1}")
        result = fba.analyzed_reaction_objective_coupling(mini_model, sol)
        assert isinstance(result, dict), (
            "analyzed_reaction_objective_coupling should return a dict"
        )


# ── 7. KBModelUtils test ────────────────────────────────────────────────


class TestKBModelUtils:
    """Test 7 from PRD §7.2 — KBModelUtils._check_and_convert_model."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("modelseedpy", reason="modelseedpy required")
        pytest.importorskip("cobrakbase", reason="cobrakbase required")

    @pytest.fixture
    def model_utils(self):
        from kbutillib.kb_model_utils import KBModelUtils

        return KBModelUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )

    def test_check_and_convert_model_returns_msmodelutil(self, model_utils, mini_model):
        """_check_and_convert_model(cobra_model) returns MSModelUtil."""
        from modelseedpy.core.msmodelutl import MSModelUtil

        result = model_utils._check_and_convert_model(mini_model)
        assert isinstance(result, MSModelUtil), (
            f"Expected MSModelUtil, got {type(result).__name__}"
        )


# ── 8. ModelStandardizationUtils test ────────────────────────────────────


class TestModelStandardizationUtils:
    """Test 8 from PRD §7.2 — model_standardization runs without error."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("modelseedpy", reason="modelseedpy required")

    @pytest.fixture
    def std_utils(self):
        from kbutillib.model_standardization_utils import ModelStandardizationUtils

        return ModelStandardizationUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )

    @pytest.mark.xfail(
        reason=(
            "P0 bug — ModelStandardizationUtils inherits MSBiochemUtils but calls "
            "_check_and_convert_model which lives on KBModelUtils. Fixed in Task 2. "
            "See PRD §3."
        ),
        strict=False,
    )
    def test_model_standardization_runs_without_error(self, std_utils, mini_model):
        """model_standardization(mini_model) runs without error."""
        result = std_utils.model_standardization(mini_model)
        assert result is not None


# ── 9. ThermoUtils test ──────────────────────────────────────────────────


class TestThermoUtils:
    """Test 9 from PRD §7.2 — ThermoUtils.get_compound_deltag."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("modelseedpy", reason="modelseedpy required for biochem_utils lazy-load")

    @pytest.fixture
    def thermo(self):
        from kbutillib.thermo_utils import ThermoUtils

        return ThermoUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )

    def test_get_compound_deltag_returns_float_or_none(self, thermo):
        """get_compound_deltag('cpd00001') returns a float or None."""
        result = thermo.get_compound_deltag("cpd00001")
        assert result is None or isinstance(result, float), (
            f"Expected float or None, got {type(result).__name__}: {result}"
        )


# ── 10. EscherUtils test ─────────────────────────────────────────────────


class TestEscherUtils:
    """Test 10 from PRD §7.2 — EscherUtils.list_available_maps."""

    @pytest.fixture(autouse=True)
    def _require_deps(self):
        pytest.importorskip("modelseedpy", reason="modelseedpy required")
        pytest.importorskip("cobrakbase", reason="cobrakbase required")

    @pytest.fixture
    def escher(self):
        from kbutillib.escher_utils import EscherUtils

        return EscherUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )

    def test_list_available_maps_returns_non_empty(self, escher):
        """list_available_maps() returns a non-empty list."""
        maps = escher.list_available_maps()
        assert isinstance(maps, list), f"Expected list, got {type(maps).__name__}"
        assert len(maps) > 0, "Expected at least one escher map"


# ── 11-12. KBWSUtils tests ──────────────────────────────────────────────


class TestKBWSUtils:
    """Tests 11-12 from PRD §7.2 — KBWSUtils invariants."""

    @pytest.mark.kbase
    def test_ws_client_constructs_with_valid_token(self):
        """ws_client constructs without error given a valid token."""
        from kbutillib.kb_ws_utils import KBWSUtils

        ws = KBWSUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )
        client = ws.ws_client()
        assert client is not None, "ws_client() should return a client"

    def test_is_ref_valid_and_invalid(self):
        """is_ref('12345/6/7') returns True, is_ref('foo') returns False."""
        from kbutillib.kb_ws_utils import KBWSUtils

        ws = KBWSUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )
        assert ws.is_ref("12345/6/7") is True, "12345/6/7 should be a valid ref"
        assert ws.is_ref("foo") is False, "'foo' should not be a valid ref"


# ── 13-14. KBGenomeUtils tests ──────────────────────────────────────────


class TestKBGenomeUtils:
    """Tests 13-14 from PRD §7.2 — KBGenomeUtils pure-logic methods."""

    @pytest.fixture
    def genome(self):
        from kbutillib.kb_genome_utils import KBGenomeUtils

        return KBGenomeUtils(
            config_file=False,
            token_file=None,
            kbase_token_file=None,
            token="fake-kbase-token-for-tests",
        )

    def test_reverse_complement(self, genome):
        """reverse_complement('ATCG') returns 'CGAT'."""
        result = genome.reverse_complement("ATCG")
        assert result == "CGAT", f"Expected 'CGAT', got '{result}'"

    def test_translate_sequence(self, genome):
        """translate_sequence('ATGATGATG') returns expected amino acid string."""
        result = genome.translate_sequence("ATGATGATG")
        # ATG → M, ATG → M, ATG → M
        assert result == "MMM", f"Expected 'MMM', got '{result}'"


# ── 15. KBAnnotationUtils test ───────────────────────────────────────────


class TestKBAnnotationUtils:
    """Test 15 from PRD §7.2 — translate_term_to_modelseed."""

    @pytest.fixture
    def annotation(self):
        from pathlib import Path

        from kbutillib.kb_annotation_utils import KBAnnotationUtils

        # The constructor reads data files from cb_annotation_ontology_api;
        # skip if the sibling repo isn't cloned alongside KBUtilLib.
        try:
            return KBAnnotationUtils(
                config_file=False,
                token_file=None,
                kbase_token_file=None,
                token="fake-kbase-token-for-tests",
            )
        except FileNotFoundError:
            pytest.skip(
                "cb_annotation_ontology_api data files not available "
                "(sibling repo not cloned)"
            )

    def test_translate_term_to_modelseed_known_term(self, annotation):
        """translate_term_to_modelseed(term) returns ModelSEED ID for known term."""
        # Use a well-known MSRXN term; for an MSRXN: term that exists in the
        # alias hash, the code returns its mapped list directly.
        result = annotation.translate_term_to_modelseed("MSRXN:rxn00001")
        assert isinstance(result, list), f"Expected list, got {type(result).__name__}"
        # For an MSRXN: prefixed term that is in the alias hash, we get results
        assert len(result) > 0, (
            "translate_term_to_modelseed should return at least one result for "
            "a known MSRXN term"
        )


# ── 16. MMSeqsUtils test ────────────────────────────────────────────────


class TestMMSeqsUtils:
    """Test 16 from PRD §7.2 — MMSeqsUtils constructor."""

    def test_constructor_succeeds_when_available(self):
        """Constructor succeeds when mmseqs2 is available."""
        try:
            subprocess.run(
                ["mmseqs", "--version"],
                capture_output=True,
                check=True,
                timeout=10,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            pytest.skip("mmseqs2 binary not available")

        from kbutillib.mmseqs_utils import MMSeqsUtils

        utils = MMSeqsUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )
        assert utils is not None


# ── 17. SKANIUtils test ──────────────────────────────────────────────────


class TestSKANIUtils:
    """Test 17 from PRD §7.2 — SKANIUtils constructor."""

    def test_constructor_succeeds_when_available(self):
        """Constructor succeeds when skani is available."""
        try:
            subprocess.run(
                ["skani", "--version"],
                capture_output=True,
                check=True,
                timeout=10,
            )
        except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
            pytest.skip("skani binary not available")

        from kbutillib.skani_utils import SKANIUtils

        utils = SKANIUtils(
            config_file=False, token_file=None, kbase_token_file=None
        )
        assert utils is not None


# ── 18-19. KBUtilLib facade tests (Task 2 stubs) ────────────────────────


class TestKBUtilLibFacade:
    """Tests 18-19 from PRD §7.2 — KBUtilLib facade (lands in Task 2)."""

    @pytest.mark.skip(reason="KBUtilLib facade lands in Task 2 of the composition refactor")
    def test_facade_fba_returns_impl(self):
        """kbu.fba returns MSFBAUtilsImpl instance."""
        from kbutillib import KBUtilLib

        kbu = KBUtilLib()
        from kbutillib.ms_fba_utils import MSFBAUtilsImpl

        assert isinstance(kbu.fba, MSFBAUtilsImpl)

    @pytest.mark.skip(reason="KBUtilLib facade lands in Task 2 of the composition refactor")
    def test_facade_biochem_lazy_singleton(self):
        """kbu.biochem is same object on second access (lazy singleton)."""
        from kbutillib import KBUtilLib

        kbu = KBUtilLib()
        first = kbu.biochem
        second = kbu.biochem
        assert first is second
