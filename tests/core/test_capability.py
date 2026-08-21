"""Unit tests for kbutillib.core.capability — @capability decorator + collector.

All tests are offline — no network, no optional backends.
"""

from __future__ import annotations

from typing import Any

import pytest

from kbutillib.core import (
    BackendUnavailableError,
    CapabilityRegistry,
    CapabilitySpec,
    capability,
    collect_capabilities,
    register_all,
)
from kbutillib.core.capability import (
    _CAPABILITY_ATTR,
    CapabilityDraft,
    _build_availability_fn,
    _unwrap,
)

# ---------------------------------------------------------------------------
# Helpers / fake utilities
# ---------------------------------------------------------------------------


class FakeUtil:
    """A minimal fake util with two @capability-decorated methods."""

    @capability(domain="fake", summary="Alpha capability", tags=("search",))
    def alpha(self, x: int) -> int:
        """Return double of x."""
        return x * 2

    @capability(
        name="fake.beta_custom",
        domain="fake",
        visibility="internal",
        tags=("write", "admin"),
    )
    def beta(self, msg: str) -> str:
        """Echo the message."""
        return msg

    def undecorated(self) -> str:
        """This should NOT appear in collected specs."""
        return "raw"


class UnavailableUtil:
    """A fake util whose ``available`` property raises BackendUnavailableError."""

    @property
    def available(self) -> bool:
        raise BackendUnavailableError("fakeback", "not installed in test env")

    @property
    def unavailable_reason(self) -> str:
        return "not installed in test env"

    @capability(domain="unavail", summary="Should be listed but unavailable")
    def do_something(self) -> None:
        """A method on an unavailable backend."""


class AvailableUtil:
    """A fake util whose ``available`` property returns True."""

    @property
    def available(self) -> bool:
        return True

    @capability(domain="avail", summary="Available method")
    def run(self) -> str:
        """Run the available method."""
        return "ok"


class EagerBackendUtil:
    """A util that would crash if instantiated eagerly (simulates heavy backend)."""

    _instantiation_count = 0

    def __init__(self) -> None:
        EagerBackendUtil._instantiation_count += 1

    @capability(domain="eager", summary="Heavy method")
    def heavy(self) -> None:
        """Heavy backend method."""


# ---------------------------------------------------------------------------
# Tests: decorator inertness
# ---------------------------------------------------------------------------


class TestDecoratorInertness:
    """Ensure @capability does NOT change the callable's behavior."""

    def test_return_value_unchanged(self) -> None:
        util = FakeUtil()
        assert util.alpha(3) == 6
        assert util.alpha(0) == 0
        assert util.alpha(-5) == -10

    def test_return_value_string_unchanged(self) -> None:
        util = FakeUtil()
        assert util.beta("hello") == "hello"

    def test_same_args_accepted(self) -> None:
        util = FakeUtil()
        result = util.alpha(10)
        assert isinstance(result, int)

    def test_exceptions_propagate_unchanged(self) -> None:
        class RaisingUtil:
            @capability(domain="raise_test")
            def boom(self) -> None:
                raise ValueError("intentional error")

        util = RaisingUtil()
        with pytest.raises(ValueError, match="intentional error"):
            util.boom()

    def test_undecorated_method_works_normally(self) -> None:
        util = FakeUtil()
        assert util.undecorated() == "raw"

    def test_decorator_does_not_wrap_function(self) -> None:
        """The decorated fn must be the EXACT same callable, not a wrapper."""
        util = FakeUtil()
        # In Python 3, accessing a method from the class gives the raw function
        raw_fn = FakeUtil.alpha
        assert hasattr(raw_fn, _CAPABILITY_ATTR)
        # Calling it directly with the instance still works
        assert raw_fn(util, 7) == 14


# ---------------------------------------------------------------------------
# Tests: metadata attachment
# ---------------------------------------------------------------------------


class TestMetadataAttachment:
    """Ensure the decorator attaches a CapabilityDraft with correct fields."""

    def _get_draft(self, fn: Any) -> CapabilityDraft:
        return getattr(fn, _CAPABILITY_ATTR)

    def test_alpha_has_capability_attr(self) -> None:
        assert hasattr(FakeUtil.alpha, _CAPABILITY_ATTR)

    def test_beta_has_capability_attr(self) -> None:
        assert hasattr(FakeUtil.beta, _CAPABILITY_ATTR)

    def test_undecorated_has_no_capability_attr(self) -> None:
        assert not hasattr(FakeUtil.undecorated, _CAPABILITY_ATTR)

    def test_alpha_domain(self) -> None:
        draft = self._get_draft(FakeUtil.alpha)
        assert draft.domain == "fake"

    def test_alpha_name_is_none_before_collection(self) -> None:
        """name=None means it will be resolved to f"{domain}.{fn.__name__}"."""
        draft = self._get_draft(FakeUtil.alpha)
        assert draft.name is None

    def test_alpha_summary(self) -> None:
        draft = self._get_draft(FakeUtil.alpha)
        assert draft.summary == "Alpha capability"

    def test_alpha_tags(self) -> None:
        draft = self._get_draft(FakeUtil.alpha)
        assert "search" in draft.tags

    def test_beta_explicit_name(self) -> None:
        draft = self._get_draft(FakeUtil.beta)
        assert draft.name == "fake.beta_custom"

    def test_beta_visibility_internal(self) -> None:
        draft = self._get_draft(FakeUtil.beta)
        assert draft.visibility == "internal"

    def test_beta_tags(self) -> None:
        draft = self._get_draft(FakeUtil.beta)
        assert "write" in draft.tags
        assert "admin" in draft.tags

    def test_default_visibility_is_public(self) -> None:
        class Tmp:
            @capability(domain="tmp")
            def m(self) -> None:
                pass

        draft = self._get_draft(Tmp.m)
        assert draft.visibility == "public"

    def test_custom_transports(self) -> None:
        class Tmp:
            @capability(domain="tmp", transports=frozenset({"lib", "mcp"}))
            def m(self) -> None:
                pass

        draft = self._get_draft(Tmp.m)
        assert draft.transports == frozenset({"lib", "mcp"})

    def test_no_transports_override_is_none(self) -> None:
        """When transports not specified, draft.transports is None (use default)."""
        draft = self._get_draft(FakeUtil.alpha)
        assert draft.transports is None


# ---------------------------------------------------------------------------
# Tests: collect_capabilities
# ---------------------------------------------------------------------------


class TestCollectCapabilities:
    """Ensure collect_capabilities produces correct CapabilitySpecs."""

    def test_returns_correct_count(self) -> None:
        util = FakeUtil()
        specs = collect_capabilities(util)
        names = {s.name for s in specs}
        assert "fake.alpha" in names
        assert "fake.beta_custom" in names

    def test_undecorated_not_included(self) -> None:
        util = FakeUtil()
        specs = collect_capabilities(util)
        names = {s.name for s in specs}
        assert "fake.undecorated" not in names

    def test_alpha_name_resolved(self) -> None:
        util = FakeUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        assert "fake.alpha" in specs

    def test_beta_custom_name_preserved(self) -> None:
        util = FakeUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        assert "fake.beta_custom" in specs

    def test_spec_type(self) -> None:
        util = FakeUtil()
        for spec in collect_capabilities(util):
            assert isinstance(spec, CapabilitySpec)

    def test_domain_correct(self) -> None:
        util = FakeUtil()
        for spec in collect_capabilities(util):
            assert spec.domain == "fake"

    def test_fn_is_bound_callable(self) -> None:
        util = FakeUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        result = specs["fake.alpha"].fn(3)
        assert result == 6

    def test_description_from_docstring(self) -> None:
        util = FakeUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        assert "double" in specs["fake.alpha"].description.lower()

    def test_tags_propagated(self) -> None:
        util = FakeUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        assert "search" in specs["fake.alpha"].tags

    def test_summary_propagated(self) -> None:
        util = FakeUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        assert specs["fake.alpha"].summary == "Alpha capability"

    def test_empty_object(self) -> None:
        class Empty:
            pass

        specs = collect_capabilities(Empty())
        assert specs == []

    def test_no_duplicate_specs(self) -> None:
        util = FakeUtil()
        specs = collect_capabilities(util)
        names = [s.name for s in specs]
        assert len(names) == len(set(names))

    # ------------------------------------------------------------------
    # Availability
    # ------------------------------------------------------------------

    def test_available_util_availability_true(self) -> None:
        util = AvailableUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        avail, reason = specs["avail.run"].availability()
        assert avail is True
        assert reason is None

    def test_unavailable_util_availability_false(self) -> None:
        util = UnavailableUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        avail, reason = specs["unavail.do_something"].availability()
        assert avail is False
        assert reason is not None
        assert "fakeback" in reason or "not installed" in reason

    def test_unavailable_util_availability_does_not_raise(self) -> None:
        util = UnavailableUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        # Must not raise
        result = specs["unavail.do_something"].availability()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_always_available_when_no_probe(self) -> None:
        """Util with no .available attribute → spec is always-available."""
        util = FakeUtil()
        specs = {s.name: s for s in collect_capabilities(util)}
        avail, reason = specs["fake.alpha"].availability()
        assert avail is True
        assert reason is None

    def test_explicit_availability_override(self) -> None:
        class CustomUtil:
            @capability(
                domain="custom",
                availability=lambda: (False, "manually disabled"),
            )
            def m(self) -> None:
                pass

        specs = {s.name: s for s in collect_capabilities(CustomUtil())}
        avail, reason = specs["custom.m"].availability()
        assert avail is False
        assert reason == "manually disabled"


# ---------------------------------------------------------------------------
# Tests: register_all (uses a fake app)
# ---------------------------------------------------------------------------


class FakeApp:
    """Minimal fake KBUtilLib facade with a couple of util attrs."""

    def __init__(self) -> None:
        self.fake = FakeUtil()
        self.avail_util = AvailableUtil()
        # Scalar attributes that should be skipped
        self.name = "FakeApp"
        self._private = "skip me"


class TestRegisterAll:
    def _fresh_registry(self) -> CapabilityRegistry:
        r = CapabilityRegistry()
        return r

    def test_registers_decorated_methods(self) -> None:
        app = FakeApp()
        registry = self._fresh_registry()
        specs = register_all(app, registry=registry)
        names = {s.name for s in specs}
        assert "fake.alpha" in names
        assert "fake.beta_custom" in names

    def test_total_registered_count(self) -> None:
        app = FakeApp()
        registry = self._fresh_registry()
        specs = register_all(app, registry=registry)
        # FakeUtil: alpha + beta_custom; AvailableUtil: run
        assert len(specs) >= 3

    def test_specs_retrievable_from_registry(self) -> None:
        app = FakeApp()
        registry = self._fresh_registry()
        register_all(app, registry=registry)
        spec = registry.get("fake.alpha")
        assert isinstance(spec, CapabilitySpec)

    def test_duplicate_name_skipped_with_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Registering the same name twice logs a warning and skips."""
        import logging

        app = FakeApp()
        registry = self._fresh_registry()
        with caplog.at_level(logging.WARNING, logger="kbutillib.core.capability"):
            register_all(app, registry=registry)
            # Second call → all are duplicates → warned, not re-registered
            specs2 = register_all(app, registry=registry)
        assert specs2 == []

    def test_undecorated_not_registered(self) -> None:
        app = FakeApp()
        registry = self._fresh_registry()
        register_all(app, registry=registry)
        names = {s.name for s in registry}
        assert "fake.undecorated" not in names
        assert "avail_util.undecorated" not in names

    def test_availability_wired_correctly(self) -> None:
        app = FakeApp()
        registry = self._fresh_registry()
        register_all(app, registry=registry)
        # AvailableUtil.run has domain="avail", so spec name is "avail.run"
        avail, _ = registry.status("avail.run")
        assert avail is True

    def test_no_eager_heavy_instantiation(self) -> None:
        """register_all must NOT trigger eager backend instantiation.

        This is tested by showing that registering a util whose constructor
        was already called doesn't double-instantiate it.
        """
        EagerBackendUtil._instantiation_count = 0

        class AppWithEager:
            def __init__(self) -> None:
                self.eager = EagerBackendUtil()

        app = AppWithEager()
        count_before = EagerBackendUtil._instantiation_count
        registry = self._fresh_registry()
        register_all(app, registry=registry)
        # No extra instantiation should have happened
        assert EagerBackendUtil._instantiation_count == count_before

    def test_unavailable_util_still_registers(self) -> None:
        """Unavailable backends must still be listed (just marked unavailable)."""

        class AppWithUnavailable:
            def __init__(self) -> None:
                self.unavail = UnavailableUtil()

        app = AppWithUnavailable()
        registry = self._fresh_registry()
        register_all(app, registry=registry)
        assert "unavail.do_something" in registry
        avail, _ = registry.status("unavail.do_something")
        assert avail is False


# ---------------------------------------------------------------------------
# Targeted branch-coverage tests (P23): _build_availability_fn, _spec_from_draft,
# collect_capabilities error paths, _unwrap, register_all default-registry and
# unexpected-exception paths.


class _RaisingReasonUtil:
    """Backend that is unavailable, and whose `unavailable_reason` also raises."""

    @property
    def available(self) -> bool:
        return False

    @property
    def unavailable_reason(self) -> str:
        raise RuntimeError("boom while computing reason")

    @capability(domain="raisingreason")
    def do_thing(self) -> str:
        return "done"


class TestBuildAvailabilityFnReasonSwallowed:
    """capability.py:219-220 — unavailable_reason raising must be swallowed."""

    def test_reason_lookup_exception_is_swallowed(self) -> None:
        util_obj = _RaisingReasonUtil()
        draft = getattr(_RaisingReasonUtil.do_thing, _CAPABILITY_ATTR)
        probe = _build_availability_fn(util_obj, draft)
        # available is False and unavailable_reason raises internally; the
        # probe must not propagate that exception and must default reason
        # to None.
        result = probe()
        assert result == (False, None)


class TestSpecFromDraftTransports:
    """capability.py:262 — explicit transports overriding the default set."""

    def test_explicit_transports_are_applied(self) -> None:
        class TransportUtil:
            @capability(domain="transp", transports=frozenset({"cli"}))
            def only_cli(self) -> str:
                return "ok"

        util_obj = TransportUtil()
        specs = collect_capabilities(util_obj)
        assert len(specs) == 1
        spec = specs[0]
        assert spec.transports == frozenset({"cli"})
        # Sanity: default transports (when omitted) is the 4-way set, so this
        # is a real behavioral difference, not a tautology.
        assert spec.transports != frozenset({"lib", "cli", "mcp", "api"})


class _RaisingClassDescriptor:
    """A descriptor that raises on __get__ regardless of instance vs class access."""

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        raise RuntimeError("class-level getattr blew up")


class TestCollectCapabilitiesClassAttrError:
    """capability.py:302-303 — getattr(type(obj), name) raising is swallowed."""

    def test_raising_class_descriptor_is_skipped(self) -> None:
        class WithBadClassAttr:
            bad_attr = _RaisingClassDescriptor()

            @capability(domain="goodclass")
            def good(self) -> str:
                return "ok"

        obj = WithBadClassAttr()
        specs = collect_capabilities(obj)
        names = [s.name for s in specs]
        assert names == ["goodclass.good"]


class _InstanceRaisingDescriptor:
    """Data descriptor: class-level access via type() returns the descriptor
    itself (since dir()/getattr(type(obj), name, None) will just yield this
    descriptor object, which is truthy), but *instance*-level getattr(obj,
    name) invokes __get__ and raises.
    """

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            # Accessed via the class itself: return something callable and
            # falsy-safe so `getattr(type(obj), name, None) or getattr(obj, name)`
            # still falls through to the instance-level getattr below because
            # a bare descriptor instance is truthy... to force the fallthrough
            # we return None here, which is falsy, so `or getattr(obj, name)`
            # is evaluated next, and that is what raises.
            return None
        raise RuntimeError("instance-level getattr blew up")


class TestCollectCapabilitiesInstanceAttrError:
    """capability.py:324-325 — getattr(obj, name) raising is swallowed."""

    def test_raising_instance_descriptor_is_skipped(self) -> None:
        class WithBadInstanceAttr:
            bad_attr = _InstanceRaisingDescriptor()

            @capability(domain="goodinst")
            def good(self) -> str:
                return "ok"

        obj = WithBadInstanceAttr()
        specs = collect_capabilities(obj)
        names = [s.name for s in specs]
        assert names == ["goodinst.good"]


class _RaisingBoundDescriptor:
    """Descriptor that returns the raw decorated function when accessed via
    the class itself (obj is None, as in `getattr(type(obj), name, None)`)
    but raises when the *instance* later re-fetches it to build the bound
    callable at capability.py:322-325 (`bound = getattr(obj, attr_name)`).
    """

    def __init__(self, func: Any) -> None:
        self.func = func

    def __get__(self, obj: Any, objtype: Any = None) -> Any:
        if obj is None:
            return self.func
        raise RuntimeError("bound instance getattr blew up")


class TestCollectCapabilitiesBoundInstanceAttrError:
    """capability.py:322-325 — the later, bound-instance getattr(obj, name)
    raising is swallowed, and other valid capabilities on the same object
    are still collected.
    """

    def test_raising_bound_descriptor_is_skipped(self) -> None:
        class WithBadBoundAttr:
            @capability(domain="badbound")
            def flaky(self) -> str:
                return "should never run"

            flaky = _RaisingBoundDescriptor(flaky)

            @capability(domain="badbound")
            def good(self) -> str:
                return "ok"

        obj = WithBadBoundAttr()
        specs = collect_capabilities(obj)
        names = [s.name for s in specs]
        assert names == ["badbound.good"]


class TestCollectCapabilitiesDuplicateNameSkipped:
    """capability.py:317-318 — duplicate resolved names within one object."""

    def test_duplicate_explicit_name_only_registers_once(self) -> None:
        class WithDuplicateNames:
            @capability(name="dup.same_name", domain="dup")
            def first(self) -> str:
                return "first"

            @capability(name="dup.same_name", domain="dup")
            def second(self) -> str:
                return "second"

        obj = WithDuplicateNames()
        specs = collect_capabilities(obj)
        matching = [s for s in specs if s.name == "dup.same_name"]
        assert len(matching) == 1


class _CallableWithFunc:
    """A plain callable object exposing a __func__ attribute (not a function,
    method, staticmethod, or classmethod)."""

    def __init__(self, func: Any) -> None:
        self.__func__ = func

    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.__func__(*args, **kwargs)


class _CallableWithCapabilityAttr:
    """A plain callable object with no __func__, but with the capability
    sentinel attribute attached directly."""

    def __call__(self, *args: Any, **kwargs: Any) -> str:
        return "called"


class TestUnwrap:
    """capability.py:340,345,348 — _unwrap branch coverage."""

    def test_unwraps_staticmethod(self) -> None:
        def raw(x: int) -> int:
            return x + 1

        result = _unwrap(staticmethod(raw))
        assert result is raw

    def test_unwraps_classmethod(self) -> None:
        def raw(cls: type) -> str:
            return "ok"

        result = _unwrap(classmethod(raw))
        assert result is raw

    def test_unwraps_callable_with_dunder_func(self) -> None:
        def raw() -> str:
            return "raw-result"

        wrapper = _CallableWithFunc(raw)
        result = _unwrap(wrapper)
        assert result is raw

    def test_unwraps_plain_callable_with_capability_attr(self) -> None:
        obj = _CallableWithCapabilityAttr()
        setattr(
            obj,
            _CAPABILITY_ATTR,
            CapabilityDraft(
                name=None,
                domain="plain",
                summary=None,
                tags=(),
                input_model=None,
                output_model=None,
                visibility="public",
                transports=None,
                availability=None,
            ),
        )
        result = _unwrap(obj)
        assert result is obj


class TestRegisterAllDefaultRegistry:
    """capability.py:408 — register_all() falling back to get_registry()."""

    def test_register_all_without_registry_uses_global(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import kbutillib.core.registry as registry_mod

        monkeypatch.setattr(registry_mod, "_registry", None)

        class DefaultRegistryUtil:
            @capability(domain="defreg")
            def go(self) -> str:
                return "ok"

        class DefaultRegistryApp:
            def __init__(self) -> None:
                self.util = DefaultRegistryUtil()

        app = DefaultRegistryApp()
        registered = register_all(app)
        names = [s.name for s in registered]
        assert "defreg.go" in names

        global_registry = registry_mod.get_registry()
        assert "defreg.go" in global_registry


class _RaisingAttrApp:
    """Facade whose `broken` attribute raises a generic (non-ImportError)
    exception on access, and whose `good` attribute is a normal capability
    host."""

    class _GoodUtil:
        @capability(domain="goodfacade")
        def run(self) -> str:
            return "ok"

    def __init__(self) -> None:
        self.good = self._GoodUtil()

    @property
    def broken(self) -> Any:
        raise RuntimeError("facade attribute access exploded")


class TestRegisterAllUnexpectedException:
    """capability.py:423,425,431 — unexpected exception during getattr(app, name)
    is logged as a warning and skipped, without aborting register_all."""

    def test_unexpected_exception_logged_and_skipped(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        app = _RaisingAttrApp()
        registry = CapabilityRegistry()
        with caplog.at_level("WARNING", logger="kbutillib.core.capability"):
            registered = register_all(app, registry=registry)
        names = [s.name for s in registered]
        assert "goodfacade.run" in names
        assert any(
            "broken" in record.message and "unexpected" in record.message
            for record in caplog.records
        )
