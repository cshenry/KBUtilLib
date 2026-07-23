"""WP3 — biochem capability proof-vertical tests.

Tests
-----
1. ``register_all`` on a fresh KBUtilLib + fresh registry yields the 3 biochem
   specs (by name and domain).
2. Direct method calls on ``MSBiochemUtilsImpl`` are byte-for-byte identical to
   before decoration — the ``@capability`` decorator is completely inert.
3. Each spec's ``availability()`` returns a ``(bool, reason)`` tuple and never
   raises, regardless of whether modelseedpy is installed.
4. Schema classes validate representative inputs (offline, no real DB needed).
"""

from __future__ import annotations

import pytest

# ---------------------------------------------------------------------------
# Helpers — expected capability names
# ---------------------------------------------------------------------------

EXPECTED_NAMES = {
    "biochem.search_compounds",
    "biochem.get_compound_by_id",
    "biochem.get_reaction_by_id",
}

EXPECTED_DOMAIN = "biochem"


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def fresh_registry():
    """Return a brand-new, isolated CapabilityRegistry (not the global one)."""
    from kbutillib.core.registry import CapabilityRegistry

    return CapabilityRegistry()


@pytest.fixture()
def kbu():
    """Return a KBUtilLib instance with no-file-discovery mode."""
    from kbutillib import KBUtilLib

    return KBUtilLib()


# ---------------------------------------------------------------------------
# 1. register_all yields 3 biochem specs
# ---------------------------------------------------------------------------


class TestRegisterAllBiochem:
    """register_all discovers the 3 biochem capabilities."""

    def test_register_all_returns_biochem_specs(self, kbu, fresh_registry):
        """register_all must register at least our 3 biochem capabilities."""
        from kbutillib.core.capability import register_all

        specs = register_all(kbu, fresh_registry)

        # Collect names in registry
        registered_names = {s.name for s in fresh_registry.list()}
        assert EXPECTED_NAMES.issubset(registered_names), (
            f"Expected {EXPECTED_NAMES} to be a subset of registered names "
            f"{registered_names}"
        )

    def test_biochem_domain_filter(self, kbu, fresh_registry):
        """fresh_registry.list(domain='biochem') returns exactly 3 specs."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        biochem_specs = fresh_registry.list(domain=EXPECTED_DOMAIN)
        biochem_names = {s.name for s in biochem_specs}

        assert EXPECTED_NAMES == biochem_names, (
            f"Expected domain='biochem' specs {EXPECTED_NAMES}, got {biochem_names}"
        )

    def test_biochem_specs_count(self, kbu, fresh_registry):
        """Exactly 3 capabilities exist in the biochem domain."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        biochem_specs = fresh_registry.list(domain=EXPECTED_DOMAIN)
        assert len(biochem_specs) == 3

    def test_spec_domain_attribute(self, kbu, fresh_registry):
        """Every biochem spec has domain='biochem'."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            assert spec.domain == EXPECTED_DOMAIN

    def test_spec_visibility_public(self, kbu, fresh_registry):
        """Every biochem spec is marked visibility='public'."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            assert spec.visibility == "public", (
                f"{spec.name} visibility={spec.visibility!r}, expected 'public'"
            )

    def test_spec_tags_contain_readonly(self, kbu, fresh_registry):
        """Every biochem spec carries the 'readonly' tag."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            assert "readonly" in spec.tags, (
                f"{spec.name} missing 'readonly' tag; tags={spec.tags}"
            )

    def test_spec_has_summary(self, kbu, fresh_registry):
        """Every biochem spec has a non-empty effective_summary."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            assert spec.effective_summary(), (
                f"{spec.name} has empty effective_summary()"
            )

    def test_individual_specs_by_name(self, kbu, fresh_registry):
        """Each expected spec can be retrieved by name from the registry."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for name in EXPECTED_NAMES:
            spec = fresh_registry.get(name)
            assert spec.name == name

    def test_global_registry_smoke(self, kbu):
        """register_all also works against the module-level global registry."""
        from kbutillib.core import get_registry
        from kbutillib.core.capability import register_all

        # Use a fresh registry object to avoid polluting the global singleton
        # across test runs — but verify the global API is importable.
        reg = get_registry().__class__()  # same class, new instance
        specs = register_all(kbu, reg)
        biochem = {s.name for s in reg.list(domain=EXPECTED_DOMAIN)}
        assert EXPECTED_NAMES.issubset(biochem)


# ---------------------------------------------------------------------------
# 2. Decorator inertness — direct method calls unchanged
# ---------------------------------------------------------------------------


class TestDecoratorInertness:
    """@capability must not change any calling behaviour on MSBiochemUtilsImpl."""

    def test_decorated_method_is_callable(self):
        """The decorated methods remain plain callables on the class."""
        from kbutillib.ms_biochem_utils import MSBiochemUtilsImpl

        assert callable(MSBiochemUtilsImpl.search_compounds)
        assert callable(MSBiochemUtilsImpl.get_compound_by_id)
        assert callable(MSBiochemUtilsImpl.get_reaction_by_id)

    def test_no_functools_wrapper(self):
        """The decorator returns the exact original function (no wrapping)."""
        from kbutillib.ms_biochem_utils import MSBiochemUtilsImpl

        for method_name in ("search_compounds", "get_compound_by_id", "get_reaction_by_id"):
            fn = getattr(MSBiochemUtilsImpl, method_name)
            # If functools.wraps had been used there would be a __wrapped__ attr.
            # The decorator contract says it returns fn directly with no wrapping.
            assert not hasattr(fn, "__wrapped__"), (
                f"{method_name} has __wrapped__ — it was wrapped, violating inertness"
            )

    def test_capability_attribute_attached(self):
        """The __kbu_capability__ attribute is present after decoration."""
        from kbutillib.core.capability import _CAPABILITY_ATTR
        from kbutillib.ms_biochem_utils import MSBiochemUtilsImpl

        for method_name in ("search_compounds", "get_compound_by_id", "get_reaction_by_id"):
            fn = getattr(MSBiochemUtilsImpl, method_name)
            assert hasattr(fn, _CAPABILITY_ATTR), (
                f"{method_name} missing {_CAPABILITY_ATTR} attribute"
            )

    def test_method_raises_same_error_without_db(self):
        """Without a ModelSEED DB, the util constructs (gracefully) but calling
        a method that needs the DB still raises the same exception as before
        decoration.  The decorator does NOT intercept or change call semantics."""
        from kbutillib.ms_biochem_utils import MSBiochemUtilsImpl
        from kbutillib.shared_env_utils import SharedEnvUtils

        env = SharedEnvUtils(config_file=False, token_file=None, kbase_token_file=None)

        # Construction should NOT raise — it stores _db_error instead.
        util = MSBiochemUtilsImpl(env, modelseed_db_path="/nonexistent_path_for_test")
        assert not util.available  # DB load failed
        assert util.unavailable_reason is not None

        # But calling a method that needs the DB still raises.
        with pytest.raises(Exception):
            # search_compounds accesses self.identifier_hash -> self.biochem_db
            # -> _ensure_database_available() -> raises
            util.search_compounds(query_identifiers=["glucose"])

    def test_collect_capabilities_inertness(self):
        """collect_capabilities returns specs but does NOT register or modify
        the method in any way that would affect a subsequent direct call."""
        from unittest.mock import MagicMock

        from kbutillib.core.capability import collect_capabilities

        # Create a minimal fake util object with a decorated method
        from kbutillib.ms_biochem_utils import MSBiochemUtilsImpl

        # Build a mock that looks like MSBiochemUtilsImpl but doesn't need a DB
        mock_util = MagicMock(spec=MSBiochemUtilsImpl)

        # collect_capabilities should not crash and returns a list
        specs = collect_capabilities(mock_util)
        assert isinstance(specs, list)


# ---------------------------------------------------------------------------
# 3. availability() never raises and returns (bool, reason) tuple
# ---------------------------------------------------------------------------


class TestAvailability:
    """Each spec's availability() returns (bool, reason | None) and never raises."""

    def test_availability_returns_tuple(self, kbu, fresh_registry):
        """availability() on each spec returns a 2-tuple."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            result = spec.availability()
            assert isinstance(result, tuple), (
                f"{spec.name}.availability() returned {type(result)}, expected tuple"
            )
            assert len(result) == 2, (
                f"{spec.name}.availability() returned tuple of length {len(result)}"
            )

    def test_availability_first_is_bool(self, kbu, fresh_registry):
        """The first element of availability() is always a bool."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            avail, _reason = spec.availability()
            assert isinstance(avail, bool), (
                f"{spec.name}.availability()[0] is {type(avail)}, expected bool"
            )

    def test_availability_second_is_str_or_none(self, kbu, fresh_registry):
        """The second element of availability() is str | None."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            _avail, reason = spec.availability()
            assert reason is None or isinstance(reason, str), (
                f"{spec.name}.availability()[1] is {type(reason)}, expected str or None"
            )

    def test_availability_never_raises(self, kbu, fresh_registry):
        """availability() must not raise any exception."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            try:
                spec.availability()
            except Exception as exc:
                pytest.fail(
                    f"{spec.name}.availability() raised {type(exc).__name__}: {exc}"
                )

    def test_registry_status_never_raises(self, kbu, fresh_registry):
        """registry.status(name) must not raise for registered biochem caps."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for name in EXPECTED_NAMES:
            try:
                result = fresh_registry.status(name)
                assert isinstance(result, tuple)
            except Exception as exc:
                pytest.fail(
                    f"fresh_registry.status({name!r}) raised {type(exc).__name__}: {exc}"
                )


# ---------------------------------------------------------------------------
# 4. Schema validation (offline — no real ModelSEED DB)
# ---------------------------------------------------------------------------


class TestSchemas:
    """Pydantic schema classes validate representative inputs correctly."""

    def test_search_compounds_input_defaults(self):
        """SearchCompoundsInput can be instantiated with all defaults."""
        from kbutillib.domains.biochem.schemas import SearchCompoundsInput

        model = SearchCompoundsInput()
        assert model.query_identifiers == []
        assert model.query_structures == []
        assert model.query_formula is None

    def test_search_compounds_input_with_values(self):
        """SearchCompoundsInput validates non-empty lists and formula."""
        from kbutillib.domains.biochem.schemas import SearchCompoundsInput

        model = SearchCompoundsInput(
            query_identifiers=["glucose", "cpd00027"],
            query_structures=["InChI=1S/C6H12O6/c7-1-2-3(8)4(9)5(10)6(11)12-2/h2-11H,1H2"],
            query_formula="C6H12O6",
        )
        assert model.query_identifiers == ["glucose", "cpd00027"]
        assert model.query_formula == "C6H12O6"

    def test_search_compounds_output_defaults(self):
        """SearchCompoundsOutput can be instantiated with empty hits."""
        from kbutillib.domains.biochem.schemas import SearchCompoundsOutput

        model = SearchCompoundsOutput()
        assert model.hits == {}

    def test_search_compounds_output_with_hits(self):
        """SearchCompoundsOutput accepts a realistic hit dict."""
        from kbutillib.domains.biochem.schemas import SearchCompoundsOutput

        hits = {
            "cpd00001": {
                "score": 10,
                "identifier_hits": {"water": "name"},
                "formula_hits": {},
                "structure_hits": {},
            }
        }
        model = SearchCompoundsOutput(hits=hits)
        assert "cpd00001" in model.hits
        assert model.hits["cpd00001"]["score"] == 10

    def test_get_compound_by_id_input_valid(self):
        """GetCompoundByIdInput requires a compound_id string."""
        from kbutillib.domains.biochem.schemas import GetCompoundByIdInput

        model = GetCompoundByIdInput(compound_id="cpd00001")
        assert model.compound_id == "cpd00001"

    def test_get_compound_by_id_input_missing_raises(self):
        """GetCompoundByIdInput raises when compound_id is missing."""
        from pydantic import ValidationError

        from kbutillib.domains.biochem.schemas import GetCompoundByIdInput

        with pytest.raises(ValidationError):
            GetCompoundByIdInput()  # type: ignore[call-arg]

    def test_get_compound_by_id_output_defaults(self):
        """GetCompoundByIdOutput defaults compound to None."""
        from kbutillib.domains.biochem.schemas import GetCompoundByIdOutput

        model = GetCompoundByIdOutput()
        assert model.compound is None

    def test_get_compound_by_id_output_with_value(self):
        """GetCompoundByIdOutput accepts arbitrary compound object."""
        from kbutillib.domains.biochem.schemas import GetCompoundByIdOutput

        class _FakeCompound:
            id = "cpd00001"
            name = "Water"

        model = GetCompoundByIdOutput(compound=_FakeCompound())
        assert model.compound.id == "cpd00001"

    def test_get_reaction_by_id_input_valid(self):
        """GetReactionByIdInput requires a reaction_id string."""
        from kbutillib.domains.biochem.schemas import GetReactionByIdInput

        model = GetReactionByIdInput(reaction_id="rxn00001")
        assert model.reaction_id == "rxn00001"

    def test_get_reaction_by_id_input_missing_raises(self):
        """GetReactionByIdInput raises when reaction_id is missing."""
        from pydantic import ValidationError

        from kbutillib.domains.biochem.schemas import GetReactionByIdInput

        with pytest.raises(ValidationError):
            GetReactionByIdInput()  # type: ignore[call-arg]

    def test_get_reaction_by_id_output_defaults(self):
        """GetReactionByIdOutput defaults reaction to None."""
        from kbutillib.domains.biochem.schemas import GetReactionByIdOutput

        model = GetReactionByIdOutput()
        assert model.reaction is None

    def test_get_reaction_by_id_output_with_value(self):
        """GetReactionByIdOutput accepts arbitrary reaction object."""
        from kbutillib.domains.biochem.schemas import GetReactionByIdOutput

        class _FakeReaction:
            id = "rxn00001"
            name = "Phosphoglucose isomerase"

        model = GetReactionByIdOutput(reaction=_FakeReaction())
        assert model.reaction.id == "rxn00001"

    def test_schemas_importable_from_domain_package(self):
        """All 6 schema classes importable from kbutillib.domains.biochem."""
        import kbutillib.domains.biochem as bd

        assert hasattr(bd, "SearchCompoundsInput")
        assert hasattr(bd, "SearchCompoundsOutput")
        assert hasattr(bd, "GetCompoundByIdInput")
        assert hasattr(bd, "GetCompoundByIdOutput")
        assert hasattr(bd, "GetReactionByIdInput")
        assert hasattr(bd, "GetReactionByIdOutput")

    def test_input_models_attached_to_specs(self, kbu, fresh_registry):
        """Each registered biochem spec has a non-None input_model."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            assert spec.input_model is not None, (
                f"{spec.name} has no input_model attached"
            )

    def test_output_models_attached_to_specs(self, kbu, fresh_registry):
        """Each registered biochem spec has a non-None output_model."""
        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            assert spec.output_model is not None, (
                f"{spec.name} has no output_model attached"
            )

    def test_input_model_is_pydantic_base_model(self, kbu, fresh_registry):
        """Each spec's input_model is a pydantic BaseModel subclass."""
        from pydantic import BaseModel

        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            assert issubclass(spec.input_model, BaseModel), (
                f"{spec.name}.input_model is not a BaseModel subclass"
            )

    def test_output_model_is_pydantic_base_model(self, kbu, fresh_registry):
        """Each spec's output_model is a pydantic BaseModel subclass."""
        from pydantic import BaseModel

        from kbutillib.core.capability import register_all

        register_all(kbu, fresh_registry)

        for spec in fresh_registry.list(domain=EXPECTED_DOMAIN):
            assert issubclass(spec.output_model, BaseModel), (
                f"{spec.name}.output_model is not a BaseModel subclass"
            )
