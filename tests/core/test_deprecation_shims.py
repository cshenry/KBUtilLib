"""Flat-submodule deprecation shim tests.

The ``domains/`` reorg deleted the flat ``kbutillib.<x>_utils`` import
surface (see ``agent-io/prds/kbutillib-reorg-integration/fullprompt.md``,
"Old -> new module mapping"). ``scripts/generate_deprecation_shims.py``
regenerates one thin, real ``.py`` module per legacy path directly under
``src/kbutillib/`` from a single table (``LEGACY_MODULE_MAP``); each
generated module warns once (module bodies only execute on first import)
and re-exports the new module's public names via ``from <new> import *``.

These tests are the load-bearing safety net for that back-compat surface:
for every legacy path in the table, importing it must (a) succeed, (b)
resolve to the *same* objects (``is``, not just ``==``) as the new
``domains.*`` path — a star-import binds the existing objects rather than
copying them — and (c) emit a ``DeprecationWarning``.
"""

from __future__ import annotations

import importlib
import importlib.util
import sys
import warnings
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _load_legacy_module_map() -> dict[str, str]:
    """Load ``LEGACY_MODULE_MAP`` from the generator script.

    Loaded by file path (not package import) so the test does not depend on
    ``scripts/`` being on ``sys.path`` — it works regardless of how pytest
    is invoked, and it keeps the old->new table single-sourced in the
    generator rather than duplicated here.
    """
    gen_path = _REPO_ROOT / "scripts" / "generate_deprecation_shims.py"
    spec = importlib.util.spec_from_file_location(
        "_generate_deprecation_shims_under_test", gen_path
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.LEGACY_MODULE_MAP


LEGACY_MODULE_MAP = _load_legacy_module_map()

# The historically-deep-imported set the task explicitly calls out as the
# minimum-required coverage (a subset of LEGACY_MODULE_MAP, asserted below).
MINIMUM_REQUIRED = {
    "ms_fba_utils",
    "kb_model_utils",
    "kb_ws_utils",
    "kb_genome_utils",
    "kb_berdl_utils",
    "ms_biochem_utils",
    "annotator_utils",
    "kb_annotation_utils",
    "shared_env_utils",
    "argo_utils",
    "thermo_utils",
}


def test_minimum_required_set_is_covered() -> None:
    """The historically-deep-imported minimum set is present in the table."""
    missing = MINIMUM_REQUIRED - set(LEGACY_MODULE_MAP)
    assert not missing, f"Minimum-required legacy modules missing a shim: {missing}"


def test_legacy_module_map_is_non_trivial() -> None:
    """Sanity: the full old->new mapping table generates many shims, not just the minimum."""
    assert len(LEGACY_MODULE_MAP) >= len(MINIMUM_REQUIRED)
    assert len(LEGACY_MODULE_MAP) >= 30


def test_shim_files_exist_on_disk_and_are_real_modules() -> None:
    """Each legacy path is a real generated .py file, not a __getattr__/meta-path trick."""
    src_root = _REPO_ROOT / "src" / "kbutillib"
    for old in LEGACY_MODULE_MAP:
        shim_path = src_root / f"{old}.py"
        assert shim_path.is_file(), f"Expected generated shim file at {shim_path}"
        text = shim_path.read_text(encoding="utf-8")
        assert "warnings" in text and "DeprecationWarning" in text
        assert "import *" in text


@pytest.fixture(autouse=True)
def _clear_shim_modules_from_cache():
    """Ensure each legacy module is (re-)imported fresh so the warning fires.

    Python only executes a module body on first import; since some other
    test (or an earlier parametrized case) may have already imported a given
    legacy module, we evict it from ``sys.modules`` first so
    ``pytest.warns(DeprecationWarning)`` reliably observes the warning here
    too.
    """
    yield
    for old in LEGACY_MODULE_MAP:
        sys.modules.pop(f"kbutillib.{old}", None)


def _import_or_skip(module_path: str):
    """Import a module, skipping if an OPTIONAL third-party dep is absent.

    A missing ``kbutillib`` module is a real failure; a missing
    third-party package (cobra, modelseedpy, ...) just means the
    optional extra is not installed in this environment.
    """
    try:
        return importlib.import_module(module_path)
    except ModuleNotFoundError as exc:  # pragma: no cover - env dependent
        if exc.name and not exc.name.startswith("kbutillib"):
            pytest.skip(f"optional dependency {exc.name!r} is not installed")
        raise


@pytest.mark.parametrize("old,new", sorted(LEGACY_MODULE_MAP.items()))
def test_legacy_import_succeeds_and_warns(old: str, new: str) -> None:
    """``from kbutillib.<old> import ...`` succeeds and emits a DeprecationWarning."""
    sys.modules.pop(f"kbutillib.{old}", None)
    with pytest.warns(DeprecationWarning, match=old):
        _import_or_skip(f"kbutillib.{old}")


@pytest.mark.parametrize("old,new", sorted(LEGACY_MODULE_MAP.items()))
def test_legacy_reexports_are_identical_objects(old: str, new: str) -> None:
    """Every public name re-exported by the shim is the SAME object (``is``) as the new module's."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old_mod = _import_or_skip(f"kbutillib.{old}")
    new_mod = _import_or_skip(f"kbutillib.{new}")

    public_names = getattr(new_mod, "__all__", None)
    if public_names is None:
        public_names = [n for n in dir(new_mod) if not n.startswith("_")]

    assert public_names, f"kbutillib.{new} exports no public names to compare"
    for name in public_names:
        assert hasattr(old_mod, name), (
            f"kbutillib.{old} did not re-export {name!r} from kbutillib.{new}"
        )
        old_obj = getattr(old_mod, name)
        new_obj = getattr(new_mod, name)
        assert old_obj is new_obj, (
            f"kbutillib.{old}.{name} is not the same object as "
            f"kbutillib.{new}.{name} (star-import should preserve identity)"
        )


def test_ms_fba_utils_class_identity_representative_sample() -> None:
    """Representative sample (explicitly required): MSFBAUtils identity across old/new paths."""
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        old_mod = _import_or_skip("kbutillib.ms_fba_utils")
    new_mod = _import_or_skip("kbutillib.domains.modeling.ms_fba_utils")
    OldMSFBAUtils = old_mod.MSFBAUtils
    NewMSFBAUtils = new_mod.MSFBAUtils

    assert OldMSFBAUtils is NewMSFBAUtils


def test_deprecation_warning_promotable_to_error() -> None:
    """Under -W error::DeprecationWarning semantics, the shim import raises."""
    sys.modules.pop("kbutillib.ms_fba_utils", None)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        with pytest.raises(DeprecationWarning):
            importlib.import_module("kbutillib.ms_fba_utils")
