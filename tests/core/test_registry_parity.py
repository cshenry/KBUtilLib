"""WP9 — registry parity test suite.

Exercises the shared conftest fixtures (fresh_registry, populated_registry,
kbutillib_app) and validates:

* register_all() populates a registry with ≥3 capabilities from a real app.
* The 3 expected biochem capabilities are present.
* Every registered spec satisfies structural invariants.
* Duplicate-name registration emits a warning via the logger.
* The populated_registry fixture itself is sane.
* CapabilitySpec schema types are correct.
* availability() semantics and status() delegation.
* register_all() does NOT touch the global singleton.
* Fixture isolation — each test gets its own registry instance.
"""

from __future__ import annotations

import logging

import pytest

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

BIOCHEM_CAPS = {
    "biochem.search_compounds",
    "biochem.get_compound_by_id",
    "biochem.get_reaction_by_id",
}

DUMMY_CAP_NAMES = {"test.foo", "test.bar", "test.baz"}


# ---------------------------------------------------------------------------
# 1. register_all — basic population
# ---------------------------------------------------------------------------


def test_register_all_yields_at_least_3_caps(fresh_registry, kbutillib_app):
    """register_all on a real KBUtilLib populates ≥3 capabilities."""
    from kbutillib.core.capability import register_all

    specs = register_all(kbutillib_app, fresh_registry)
    assert len(specs) >= 3, f"Expected ≥3 registered caps, got {len(specs)}"
    assert len(fresh_registry) >= 3


def test_register_all_returns_list_of_specs(fresh_registry, kbutillib_app):
    """register_all returns a list (not a generator or other iterable)."""
    from kbutillib.core.capability import register_all
    from kbutillib.core.registry import CapabilitySpec

    result = register_all(kbutillib_app, fresh_registry)
    assert isinstance(result, list)
    assert all(isinstance(s, CapabilitySpec) for s in result)


def test_register_all_idempotent_on_second_call(fresh_registry, kbutillib_app):
    """Calling register_all twice on the same registry logs warnings but does not raise."""
    from kbutillib.core.capability import register_all

    first = register_all(kbutillib_app, fresh_registry)
    # Second call should produce no new registrations (duplicates are skipped).
    second = register_all(kbutillib_app, fresh_registry)
    assert len(second) == 0, "Second register_all should skip all duplicates"
    assert len(fresh_registry) == len(first)


def test_register_all_does_not_touch_global_singleton(fresh_registry, kbutillib_app):
    """register_all with an explicit registry NEVER modifies get_registry() singleton."""
    from kbutillib.core.capability import register_all
    from kbutillib.core.registry import get_registry

    global_before = set(s.name for s in get_registry())
    register_all(kbutillib_app, fresh_registry)
    global_after = set(s.name for s in get_registry())
    assert global_before == global_after, (
        "register_all with explicit registry must not write to the global singleton"
    )


# ---------------------------------------------------------------------------
# 2. Biochem capabilities presence
# ---------------------------------------------------------------------------


def test_biochem_caps_present(fresh_registry, kbutillib_app):
    """The 3 biochem capabilities are registered after register_all."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    registered_names = {s.name for s in fresh_registry}
    for cap_name in BIOCHEM_CAPS:
        assert cap_name in registered_names, (
            f"Expected capability {cap_name!r} not found in registry"
        )


def test_biochem_caps_have_correct_domain(fresh_registry, kbutillib_app):
    """The biochem capabilities all belong to the 'biochem' domain."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    for name in BIOCHEM_CAPS:
        spec = fresh_registry.get(name)
        assert spec.domain == "biochem", (
            f"{name} has domain {spec.domain!r}, expected 'biochem'"
        )


def test_biochem_caps_retrievable_by_domain_filter(fresh_registry, kbutillib_app):
    """registry.list(domain='biochem') returns all 3 biochem capabilities."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    biochem_specs = fresh_registry.list(domain="biochem")
    biochem_names = {s.name for s in biochem_specs}
    assert BIOCHEM_CAPS.issubset(biochem_names), (
        f"Missing biochem caps in domain filter: {BIOCHEM_CAPS - biochem_names}"
    )


# ---------------------------------------------------------------------------
# 3. Structural invariants — every registered cap
# ---------------------------------------------------------------------------


def test_all_caps_have_name_and_callable(fresh_registry, kbutillib_app):
    """Every registered cap has a non-empty name string and a callable fn."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    for spec in fresh_registry:
        assert isinstance(spec.name, str) and spec.name, (
            f"Spec has empty or non-string name: {spec!r}"
        )
        assert callable(spec.fn), (
            f"Spec {spec.name!r} has non-callable fn: {spec.fn!r}"
        )


def test_caps_pass_schema_validation(fresh_registry, kbutillib_app):
    """Every cap's spec has correct types: name=str, domain=str, tags=tuple."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    for spec in fresh_registry:
        assert isinstance(spec.name, str), (
            f"{spec.name}: name is not str, got {type(spec.name)}"
        )
        assert isinstance(spec.domain, str), (
            f"{spec.name}: domain is not str, got {type(spec.domain)}"
        )
        assert isinstance(spec.tags, tuple), (
            f"{spec.name}: tags is not tuple, got {type(spec.tags)}"
        )
        assert isinstance(spec.transports, frozenset), (
            f"{spec.name}: transports is not frozenset, got {type(spec.transports)}"
        )
        assert spec.visibility in ("public", "internal"), (
            f"{spec.name}: visibility {spec.visibility!r} not in ('public', 'internal')"
        )


def test_unavailable_caps_have_reason(fresh_registry, kbutillib_app):
    """Caps with available=False must have a non-None unavailable_reason string."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    for spec in fresh_registry:
        available, reason = spec.availability()
        if not available:
            assert reason is not None, (
                f"{spec.name}: available=False but reason is None"
            )
            assert isinstance(reason, str), (
                f"{spec.name}: reason is not str, got {type(reason)}"
            )


def test_availability_never_raises_for_any_cap(fresh_registry, kbutillib_app):
    """spec.availability() must never raise for any registered capability."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    for spec in fresh_registry:
        try:
            result = spec.availability()
            assert isinstance(result, tuple) and len(result) == 2
        except Exception as exc:
            pytest.fail(
                f"{spec.name}.availability() raised {type(exc).__name__}: {exc}"
            )


def test_all_caps_have_transports_set(fresh_registry, kbutillib_app):
    """Every registered spec's transports is a non-empty frozenset."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    for spec in fresh_registry:
        assert isinstance(spec.transports, frozenset), (
            f"{spec.name}: transports should be frozenset"
        )
        assert len(spec.transports) > 0, (
            f"{spec.name}: transports frozenset is empty"
        )


# ---------------------------------------------------------------------------
# 4. Duplicate name warning
# ---------------------------------------------------------------------------


def test_duplicate_name_warning(fresh_registry, caplog):
    """Registering a duplicate name via register_all emits a logger warning."""
    from kbutillib.core.capability import register_all
    from kbutillib.core.registry import CapabilitySpec

    # Pre-register one biochem-named cap directly so the second register_all hits a dup.
    fresh_registry.register(
        CapabilitySpec(
            name="biochem.search_compounds",
            fn=lambda: None,
            domain="biochem",
            summary="Pre-registered stub",
        )
    )

    from kbutillib import KBUtilLib

    app = KBUtilLib()
    with caplog.at_level(logging.WARNING, logger="kbutillib.core.capability"):
        register_all(app, fresh_registry)

    # At least one warning about the duplicate name
    dup_warnings = [
        r for r in caplog.records
        if "duplicate" in r.message.lower() or "biochem.search_compounds" in r.message
    ]
    assert len(dup_warnings) >= 1, (
        "Expected a WARNING log for duplicate capability name, got none. "
        f"All log records: {[r.message for r in caplog.records]}"
    )


def test_duplicate_name_register_raises_value_error(fresh_registry):
    """Direct registry.register() of a duplicate name raises ValueError."""
    from kbutillib.core.registry import CapabilitySpec

    spec = CapabilitySpec(name="dup.test", fn=lambda: None, domain="test")
    fresh_registry.register(spec)
    with pytest.raises(ValueError, match="already registered"):
        fresh_registry.register(spec)


# ---------------------------------------------------------------------------
# 5. populated_registry fixture sanity
# ---------------------------------------------------------------------------


def test_populated_registry_has_3_caps(populated_registry):
    """The populated_registry fixture starts with exactly 3 dummy caps."""
    assert len(populated_registry) == 3


def test_populated_registry_has_expected_names(populated_registry):
    """The populated_registry fixture contains the expected dummy cap names."""
    names = {s.name for s in populated_registry}
    assert names == DUMMY_CAP_NAMES


def test_populated_registry_caps_have_callable_fn(populated_registry):
    """All caps in populated_registry have callable fn."""
    for spec in populated_registry:
        assert callable(spec.fn), f"{spec.name} fn is not callable"


def test_populated_registry_caps_have_tuple_tags(populated_registry):
    """All caps in populated_registry have tuple tags."""
    for spec in populated_registry:
        assert isinstance(spec.tags, tuple), (
            f"{spec.name}: tags is {type(spec.tags)}, expected tuple"
        )


def test_populated_registry_is_isolated_from_global(populated_registry):
    """The populated_registry fixture does not affect the global singleton."""
    from kbutillib.core.registry import get_registry

    global_reg = get_registry()
    for name in DUMMY_CAP_NAMES:
        # Dummy test caps must NOT appear in the global singleton
        assert name not in global_reg, (
            f"Dummy cap {name!r} leaked into global registry"
        )


def test_populated_registry_is_isolated_between_tests_a(populated_registry):
    """First test: adds an extra cap to populated_registry (should not affect _b)."""
    from kbutillib.core.registry import CapabilitySpec

    populated_registry.register(
        CapabilitySpec(name="extra.cap_a", fn=lambda: None, domain="extra")
    )
    assert len(populated_registry) == 4


def test_populated_registry_is_isolated_between_tests_b(populated_registry):
    """Second test: populated_registry still has exactly 3 caps (not 4 from _a)."""
    assert len(populated_registry) == 3, (
        "populated_registry leaked state from another test — fixture isolation broken"
    )


# ---------------------------------------------------------------------------
# 6. kbutillib_app fixture sanity
# ---------------------------------------------------------------------------


def test_kbutillib_app_is_kbutillib_instance(kbutillib_app):
    """kbutillib_app fixture yields a KBUtilLib instance."""
    from kbutillib import KBUtilLib

    assert isinstance(kbutillib_app, KBUtilLib)


def test_kbutillib_app_has_biochem_attr(kbutillib_app):
    """kbutillib_app.biochem returns an object (lazy-loaded util)."""
    assert hasattr(kbutillib_app, "biochem")
    util = kbutillib_app.biochem
    assert util is not None


# ---------------------------------------------------------------------------
# 7. registry.status() delegation
# ---------------------------------------------------------------------------


def test_status_delegates_to_availability(fresh_registry, kbutillib_app):
    """registry.status(name) returns same (bool, reason) as spec.availability()."""
    from kbutillib.core.capability import register_all

    register_all(kbutillib_app, fresh_registry)
    for spec in fresh_registry:
        status_result = fresh_registry.status(spec.name)
        avail_result = spec.availability()
        assert status_result == avail_result, (
            f"{spec.name}: status()={status_result} != availability()={avail_result}"
        )


def test_status_raises_capability_not_found(fresh_registry):
    """registry.status() raises CapabilityNotFound for unregistered name."""
    from kbutillib.core.errors import CapabilityNotFound

    with pytest.raises(CapabilityNotFound):
        fresh_registry.status("nonexistent.capability")
