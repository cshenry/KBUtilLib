"""Unit tests for kbutillib.core (CapabilityRegistry, CapabilitySpec, errors).

All tests are offline — no network, no optional deps.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest

from kbutillib.core import (
    BackendUnavailableError,
    CapabilityError,
    CapabilityNotFound,
    CapabilityRegistry,
    CapabilitySpec,
    KBUtilLibError,
    get_registry,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_spec(
    name: str = "test.cap",
    domain: str = "test",
    tags: tuple[str, ...] = (),
    visibility: str = "internal",
    transports: frozenset[str] | None = None,
    availability_fn: Any = None,
    fn: Any = None,
    description: str = "",
) -> CapabilitySpec:
    return CapabilitySpec(
        name=name,
        fn=fn or (lambda: None),
        domain=domain,
        tags=tags,
        visibility=visibility,
        transports=transports if transports is not None else frozenset({"lib", "cli", "mcp", "api"}),
        _availability_fn=availability_fn,
        description=description,
    )


# ---------------------------------------------------------------------------
# Error hierarchy
# ---------------------------------------------------------------------------


class TestErrors:
    def test_backend_unavailable_error_is_kbutillib_error(self) -> None:
        exc = BackendUnavailableError("rdkit", "not installed")
        assert isinstance(exc, KBUtilLibError)
        assert isinstance(exc, Exception)

    def test_backend_unavailable_error_attrs(self) -> None:
        exc = BackendUnavailableError("rdkit", "not installed")
        assert exc.backend == "rdkit"
        assert exc.reason == "not installed"

    def test_backend_unavailable_error_str(self) -> None:
        exc = BackendUnavailableError("rdkit", "not installed")
        assert "rdkit" in str(exc)
        assert "not installed" in str(exc)

    def test_backend_unavailable_error_no_args(self) -> None:
        exc = BackendUnavailableError()
        assert isinstance(exc, KBUtilLibError)
        assert "unavailable" in str(exc).lower()

    def test_capability_not_found_is_capability_error(self) -> None:
        exc = CapabilityNotFound("foo.bar")
        assert isinstance(exc, CapabilityError)
        assert isinstance(exc, KBUtilLibError)
        assert "foo.bar" in str(exc)

    def test_capability_not_found_attr(self) -> None:
        exc = CapabilityNotFound("some.cap")
        assert exc.name == "some.cap"


# ---------------------------------------------------------------------------
# CapabilitySpec
# ---------------------------------------------------------------------------


class TestCapabilitySpec:
    def test_basic_creation(self) -> None:
        spec = _make_spec(name="bio.search", domain="bio")
        assert spec.name == "bio.search"
        assert spec.domain == "bio"

    def test_frozen(self) -> None:
        spec = _make_spec()
        with pytest.raises((AttributeError, TypeError)):
            spec.name = "other"  # type: ignore[misc]

    def test_default_visibility_internal(self) -> None:
        spec = _make_spec()
        assert spec.visibility == "internal"

    def test_default_transports(self) -> None:
        spec = _make_spec()
        assert "lib" in spec.transports
        assert "mcp" in spec.transports

    def test_availability_no_fn_returns_true(self) -> None:
        spec = _make_spec()
        available, reason = spec.availability()
        assert available is True
        assert reason is None

    def test_availability_fn_returns_true(self) -> None:
        spec = _make_spec(availability_fn=lambda: True)
        available, reason = spec.availability()
        assert available is True
        assert reason is None

    def test_availability_fn_returns_false(self) -> None:
        spec = _make_spec(availability_fn=lambda: False)
        available, reason = spec.availability()
        assert available is False

    def test_availability_fn_raises_backend_unavailable(self) -> None:
        def bad() -> bool:
            raise BackendUnavailableError("rdkit", "not installed")

        spec = _make_spec(availability_fn=bad)
        available, reason = spec.availability()
        assert available is False
        assert reason is not None
        assert "rdkit" in reason or "not installed" in reason

    def test_availability_fn_raises_generic_exception(self) -> None:
        def bad() -> bool:
            raise RuntimeError("boom")

        spec = _make_spec(availability_fn=bad)
        available, reason = spec.availability()
        assert available is False
        assert reason is not None
        assert "boom" in reason

    def test_availability_never_raises(self) -> None:
        """availability() must NEVER propagate any exception."""

        def always_raises() -> bool:
            raise SystemError("catastrophic")

        spec = _make_spec(availability_fn=always_raises)
        # Should not raise:
        result = spec.availability()
        assert isinstance(result, tuple)
        assert len(result) == 2

    def test_availability_fn_returns_tuple(self) -> None:
        """availability_fn may itself return (bool, reason)."""
        spec = _make_spec(availability_fn=lambda: (False, "missing dep"))
        available, reason = spec.availability()
        assert available is False
        assert reason == "missing dep"

    def test_effective_summary_from_summary(self) -> None:
        spec = CapabilitySpec(
            name="x.y",
            fn=lambda: None,
            summary="Short summary",
            description="Longer description.",
        )
        assert spec.effective_summary() == "Short summary"

    def test_effective_summary_fallback_to_description(self) -> None:
        spec = CapabilitySpec(
            name="x.y",
            fn=lambda: None,
            description="First line.\nSecond line.",
        )
        assert spec.effective_summary() == "First line."

    def test_effective_summary_fallback_to_name(self) -> None:
        spec = CapabilitySpec(name="x.y", fn=lambda: None)
        assert spec.effective_summary() == "x.y"


# ---------------------------------------------------------------------------
# CapabilityRegistry — basic CRUD
# ---------------------------------------------------------------------------


class TestCapabilityRegistryCRUD:
    def setup_method(self) -> None:
        self.reg = CapabilityRegistry()

    def test_register_and_get(self) -> None:
        spec = _make_spec(name="bio.foo")
        self.reg.register(spec)
        retrieved = self.reg.get("bio.foo")
        assert retrieved is spec

    def test_get_missing_raises_capability_not_found(self) -> None:
        with pytest.raises(CapabilityNotFound) as exc_info:
            self.reg.get("nonexistent.cap")
        assert "nonexistent.cap" in str(exc_info.value)

    def test_duplicate_name_raises_value_error(self) -> None:
        spec = _make_spec(name="dup.cap")
        self.reg.register(spec)
        with pytest.raises(ValueError, match="already registered"):
            self.reg.register(spec)

    def test_duplicate_name_different_fn_still_raises(self) -> None:
        spec1 = _make_spec(name="dup.cap")
        spec2 = _make_spec(name="dup.cap", fn=lambda: 42)
        self.reg.register(spec1)
        with pytest.raises(ValueError):
            self.reg.register(spec2)

    def test_register_non_spec_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            self.reg.register("not a spec")  # type: ignore[arg-type]

    def test_len(self) -> None:
        assert len(self.reg) == 0
        self.reg.register(_make_spec(name="a.b"))
        assert len(self.reg) == 1
        self.reg.register(_make_spec(name="a.c"))
        assert len(self.reg) == 2

    def test_contains(self) -> None:
        self.reg.register(_make_spec(name="x.y"))
        assert "x.y" in self.reg
        assert "z.w" not in self.reg

    def test_iter(self) -> None:
        specs = [_make_spec(name=f"dom.cap{i}") for i in range(3)]
        for s in specs:
            self.reg.register(s)
        retrieved = list(self.reg)
        assert len(retrieved) == 3
        assert {s.name for s in retrieved} == {"dom.cap0", "dom.cap1", "dom.cap2"}

    def test_clear(self) -> None:
        self.reg.register(_make_spec(name="to.clear"))
        assert len(self.reg) == 1
        self.reg.clear()
        assert len(self.reg) == 0
        assert "to.clear" not in self.reg

    def test_clear_allows_reregister(self) -> None:
        spec = _make_spec(name="reuse.cap")
        self.reg.register(spec)
        self.reg.clear()
        self.reg.register(spec)  # should not raise
        assert "reuse.cap" in self.reg


# ---------------------------------------------------------------------------
# CapabilityRegistry — list / filter
# ---------------------------------------------------------------------------


class TestCapabilityRegistryFilter:
    def setup_method(self) -> None:
        self.reg = CapabilityRegistry()
        self.reg.register(_make_spec(name="bio.search", domain="bio", tags=("search",), visibility="public", transports=frozenset({"lib", "mcp"})))
        self.reg.register(_make_spec(name="bio.get", domain="bio", tags=("read",), visibility="internal"))
        self.reg.register(_make_spec(name="thermo.delta", domain="thermo", tags=("compute", "search"), visibility="public"))
        self.reg.register(_make_spec(name="thermo.predict", domain="thermo", tags=("compute",), visibility="internal", transports=frozenset({"lib"})))

    def test_list_all(self) -> None:
        assert len(self.reg.list()) == 4

    def test_filter_domain(self) -> None:
        bio = self.reg.list(domain="bio")
        assert len(bio) == 2
        assert all(s.domain == "bio" for s in bio)

    def test_filter_domain_no_match(self) -> None:
        assert self.reg.list(domain="nonexistent") == []

    def test_filter_tag(self) -> None:
        search = self.reg.list(tag="search")
        assert len(search) == 2
        assert {s.name for s in search} == {"bio.search", "thermo.delta"}

    def test_filter_visibility_public(self) -> None:
        public = self.reg.list(visibility="public")
        assert len(public) == 2
        assert all(s.visibility == "public" for s in public)

    def test_filter_visibility_internal(self) -> None:
        internal = self.reg.list(visibility="internal")
        assert len(internal) == 2

    def test_filter_transport(self) -> None:
        mcp_caps = self.reg.list(transport="mcp")
        # bio.search has mcp; bio.get and thermo.delta and thermo.predict — depends on their transports
        # bio.search: {lib, mcp}; bio.get: {lib,cli,mcp,api}; thermo.delta: {lib,cli,mcp,api}; thermo.predict: {lib}
        names = {s.name for s in mcp_caps}
        assert "bio.search" in names
        assert "thermo.predict" not in names

    def test_filter_combined(self) -> None:
        result = self.reg.list(domain="thermo", tag="compute")
        assert len(result) == 2

    def test_filter_combined_no_match(self) -> None:
        result = self.reg.list(domain="bio", tag="compute")
        assert result == []


# ---------------------------------------------------------------------------
# CapabilityRegistry — status
# ---------------------------------------------------------------------------


class TestCapabilityRegistryStatus:
    def setup_method(self) -> None:
        self.reg = CapabilityRegistry()

    def test_status_available(self) -> None:
        self.reg.register(_make_spec(name="ok.cap", availability_fn=lambda: True))
        available, reason = self.reg.status("ok.cap")
        assert available is True
        assert reason is None

    def test_status_unavailable(self) -> None:
        def bad() -> bool:
            raise BackendUnavailableError("rdkit")

        self.reg.register(_make_spec(name="bad.cap", availability_fn=bad))
        available, reason = self.reg.status("bad.cap")
        assert available is False
        assert reason is not None

    def test_status_missing_name_raises_capability_not_found(self) -> None:
        with pytest.raises(CapabilityNotFound):
            self.reg.status("nope.nope")

    def test_status_never_raises_on_availability_error(self) -> None:
        def explode() -> bool:
            raise RuntimeError("💥")

        self.reg.register(_make_spec(name="boom.cap", availability_fn=explode))
        # must not raise:
        result = self.reg.status("boom.cap")
        assert result[0] is False


# ---------------------------------------------------------------------------
# Thread safety
# ---------------------------------------------------------------------------


class TestThreadSafety:
    def test_concurrent_register_no_deadlock(self) -> None:
        reg = CapabilityRegistry()
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                reg.register(_make_spec(name=f"thread.cap{i}"))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(20)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert len(reg) == 20

    def test_concurrent_read_write(self) -> None:
        reg = CapabilityRegistry()
        for i in range(10):
            reg.register(_make_spec(name=f"base.cap{i}"))

        reads: list[int] = []
        errors: list[Exception] = []

        def reader() -> None:
            try:
                reads.append(len(reg.list()))
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=reader) for _ in range(50)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert not errors
        assert all(r >= 10 for r in reads)


# ---------------------------------------------------------------------------
# Global registry singleton
# ---------------------------------------------------------------------------


class TestGetRegistry:
    def test_returns_registry_instance(self) -> None:
        reg = get_registry()
        assert isinstance(reg, CapabilityRegistry)

    def test_singleton_same_object(self) -> None:
        reg1 = get_registry()
        reg2 = get_registry()
        assert reg1 is reg2

    def test_can_register_on_global(self) -> None:
        reg = get_registry()
        name = "__test_global_singleton_cap__"
        if name not in reg:
            reg.register(_make_spec(name=name))
        assert name in reg
        # Cleanup so other test runs aren't affected by leftover state
        # (we can't clear the global, so just verify presence)
        assert isinstance(reg.get(name), CapabilitySpec)


# ---------------------------------------------------------------------------
# Import path — core package public API
# ---------------------------------------------------------------------------


class TestImportPaths:
    def test_import_from_core_package(self) -> None:
        from kbutillib.core import (  # noqa: F401
            BackendUnavailableError,
            CapabilityError,
            CapabilityNotFound,
            CapabilityRegistry,
            CapabilitySpec,
            KBUtilLibError,
            get_registry,
        )

    def test_import_from_core_errors(self) -> None:
        from kbutillib.core.errors import (  # noqa: F401
            BackendUnavailableError,
            CapabilityError,
            CapabilityNotFound,
            KBUtilLibError,
        )

    def test_import_from_core_registry(self) -> None:
        from kbutillib.core.registry import (  # noqa: F401
            CapabilityRegistry,
            CapabilitySpec,
            get_registry,
        )

    def test_backend_unavailable_is_same_class_across_imports(self) -> None:
        from kbutillib.core import BackendUnavailableError as A
        from kbutillib.core.errors import BackendUnavailableError as B
        from kbutillib.core.registry import BackendUnavailableError as C

        assert A is B
        assert B is C

    def test_kbutillib_import_still_works(self) -> None:
        import kbutillib  # noqa: F401

        assert hasattr(kbutillib, "KBUtilLib")
