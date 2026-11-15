"""Unit tests for the FVA panel layer — on fakes, no solver, no modelseedpy.

Everything here that is worth pinning is decidable without a real solve: the
growth gate's threshold and direction, which media get an FVA attempt and which
get ``{}``, that ``growth_out`` records the non-growing media too, that the
model handed to each medium's FVA came from the PRE-LOOP snapshot rather than
from whatever the previous gate mutated, and the essentiality rule including its
relative epsilon.  The real-dependency parity against GAA's own implementation
lives in ``tests/modeling/test_fva_panel_real_dependency.py``.

The fakes' ``run_fba``/``run_fva`` are deliberately KEYWORD-ONLY.  That is not
decoration: a modelseedpy version string does not identify the code behind it,
so the layer must call in by keyword, and a positional call from
``fva_panel`` fails these tests with ``TypeError`` instead of passing quietly
and then binding to the wrong parameter against a different build.
"""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, List

import pytest

from kbutillib.domains.modeling import fva_panel


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeModel:
    """A stand-in cobra model that records what was done to it."""

    def __init__(self, tag: str, mutations: List[str] | None = None) -> None:
        self.tag = tag
        #: Media ids whose growth gate mutated this model, in order.  The real
        #: mutation is media bounds + objective constraints applied in place by
        #: ``configure_fba_formulation``; the list is a visible proxy for it.
        self.mutations: List[str] = list(mutations or [])
        self.copies = 0

    def copy(self) -> "FakeModel":
        clone = FakeModel(self.tag, self.mutations)
        clone.copies = self.copies + 1
        return clone


class FakeMdlUtl:
    """A stand-in ``MSModelUtil``."""

    def __init__(self, model: FakeModel) -> None:
        self.model = model


class FakeCobraJson:
    """``cobra.io.json`` stand-in; counts snapshots so the test can pin "once"."""

    def __init__(self) -> None:
        self.snapshot_count = 0

    def model_to_dict(self, model: FakeModel) -> Dict[str, Any]:
        self.snapshot_count += 1
        return {"tag": model.tag, "mutations": list(model.mutations)}

    def model_from_dict(self, doc: Dict[str, Any]) -> FakeModel:
        return FakeModel(doc["tag"], doc["mutations"])


class FakeSolution:
    def __init__(self, objective_value: Any) -> None:
        self.objective_value = objective_value


class FakeFBA:
    """An ``MSFBAUtils`` stand-in with scripted growth and FVA results."""

    def __init__(
        self,
        *,
        growth: Dict[str, Any],
        fva: Dict[str, Any] | None = None,
        fba_raises: tuple = (),
        fva_raises: tuple = (),
    ) -> None:
        self._growth = growth
        self._fva = fva or {}
        self._fba_raises = set(fba_raises)
        self._fva_raises = set(fva_raises)
        #: ``(media_id, run_pfba)`` per gate call, in order.
        self.fba_calls: List[tuple] = []
        #: ``(media_id, fraction_of_optimum, model_mutations, model_copies)``
        #: per FVA call, in order.
        self.fva_calls: List[tuple] = []

    def run_fba(self, *, model: Any, media: Any, run_pfba: bool) -> Any:
        self.fba_calls.append((media.id, run_pfba))
        # The real call mutates the model it is handed, in place.
        model.model.mutations.append(media.id)
        if media.id in self._fba_raises:
            raise RuntimeError(f"gate blew up on {media.id}")
        value = self._growth[media.id]
        if value is None:
            return None
        if value == "no-solution":
            return None
        if value == "none-objective":
            return FakeSolution(None)
        if value == "uncoercible":
            return FakeSolution("not-a-number")
        return FakeSolution(value)

    def run_fva(self, *, model: Any, media: Any, fraction_of_optimum: float) -> Any:
        self.fva_calls.append(
            (
                media.id,
                fraction_of_optimum,
                list(model.model.mutations),
                model.model.copies,
            )
        )
        if media.id in self._fva_raises:
            raise RuntimeError(f"fva blew up on {media.id}")
        return self._fva[media.id]


class FakeMedia:
    def __init__(self, media_id: str) -> None:
        self.id = media_id


@pytest.fixture
def stub_modelseedpy(monkeypatch):
    """Make ``modelseedpy.core.msmodelutl.MSModelUtil`` importable as a fake.

    The task venv has neither cobra nor modelseedpy, which is the point: the
    layer must be importable and unit-testable without them.  Only the one
    symbol ``_fva_one_medium`` imports is provided.
    """
    msmodelutl = types.ModuleType("modelseedpy.core.msmodelutl")

    class _MSModelUtil:
        @staticmethod
        def get(model: FakeModel) -> FakeMdlUtl:
            return FakeMdlUtl(model)

    msmodelutl.MSModelUtil = _MSModelUtil
    core = types.ModuleType("modelseedpy.core")
    core.msmodelutl = msmodelutl
    root = types.ModuleType("modelseedpy")
    root.core = core
    monkeypatch.setitem(sys.modules, "modelseedpy", root)
    monkeypatch.setitem(sys.modules, "modelseedpy.core", core)
    monkeypatch.setitem(sys.modules, "modelseedpy.core.msmodelutl", msmodelutl)
    return msmodelutl


@pytest.fixture
def cobra_json(monkeypatch) -> FakeCobraJson:
    fake = FakeCobraJson()
    monkeypatch.setattr(fva_panel, "_cobra_json_module", lambda: fake)
    return fake


def _panel(*media_ids: str) -> fva_panel.MediaPanel:
    return fva_panel.MediaPanel(
        complete=FakeMedia("Complete"),
        gapfill_media=[],
        fva_media=[(mid, FakeMedia(mid)) for mid in media_ids],
    )


def _rows(**values: float) -> Dict[str, Dict[str, float]]:
    return {rxn: {"MIN": lo, "MAX": hi} for rxn, (lo, hi) in values.items()}


# ---------------------------------------------------------------------------
# Importability — the layer must not need the model stack to be imported
# ---------------------------------------------------------------------------


def _module_level_imports(module: Any) -> set:
    """Every module name imported at MODULE SCOPE by *module*'s source.

    Asserted via the AST rather than by trying to import the absent package,
    because whether cobra is installed is a property of the venv and this is a
    property of the code: the test must mean the same thing in the task venv
    (no cobra) and in the scratch venv (cobra present).
    """
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    names = set()
    for node in tree.body:  # module scope ONLY -- lazy imports live in functions
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def _all_imports(module: Any) -> set:
    """Every module name imported at ANY depth, lazy ones included."""
    import ast
    import pathlib

    tree = ast.parse(pathlib.Path(module.__file__).read_text())
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


class TestImportability:
    def test_no_module_scope_import_of_the_model_stack(self):
        """cobra and modelseedpy are reached lazily, inside function bodies.

        The guarantee this protects is architectural: a caller must be able to
        import this module, and build and inspect a ``MediaPanel``, in an
        environment that cannot solve.  The task venv is exactly that
        environment, which is why the module-level import at the top of this
        file is itself part of the evidence.
        """
        at_module_scope = _module_level_imports(fva_panel)
        for heavy in ("cobra", "modelseedpy"):
            assert not any(
                name == heavy or name.startswith(heavy + ".")
                for name in at_module_scope
            ), f"{heavy} must not be imported at module scope"

    def test_the_model_stack_is_still_reached_somewhere(self):
        """Guards the test above against passing because the imports vanished."""
        lazily = _all_imports(fva_panel)
        assert any(name.startswith("cobra") for name in lazily)
        assert any(name.startswith("modelseedpy") for name in lazily)

    def test_media_panel_constructs_without_the_model_stack(self):
        panel = _panel("A", "B")
        assert [mid for mid, _ in panel.fva_media] == ["A", "B"]
        assert panel.gapfill_media == []
        assert panel.complete.id == "Complete"

    def test_exported_from_the_modeling_package(self):
        from kbutillib.domains.modeling import (
            MediaPanel,
            compute_fva_essentiality,
            run_fva_panel,
        )

        assert MediaPanel is fva_panel.MediaPanel
        assert run_fva_panel is fva_panel.run_fva_panel
        assert compute_fva_essentiality is fva_panel.compute_fva_essentiality


# ---------------------------------------------------------------------------
# Essentiality classification
# ---------------------------------------------------------------------------


class TestClassifyFvaEssentiality:
    @pytest.mark.parametrize(
        "fva_min,fva_max,expected",
        [
            (0.0, 0.0, "blocked"),
            (1e-12, -1e-12, "blocked"),
            (1.0, 10.0, "essential-forward"),
            (-10.0, -1.0, "essential-reverse"),
            (-5.0, 5.0, "functional"),
            (0.0, 10.0, "functional"),  # can carry zero -> not essential
            (-10.0, 0.0, "functional"),
        ],
    )
    def test_rule(self, fva_min, fva_max, expected):
        assert fva_panel.classify_fva_essentiality(fva_min, fva_max) == expected

    def test_small_but_real_flux_is_essential_not_blocked(self):
        """At small magnitudes eps is the 1e-9 absolute floor, not the relative term."""
        assert (
            fva_panel.classify_fva_essentiality(1e-7, 1e-7) == "essential-forward"
        )

    def test_relative_epsilon_scales_with_the_larger_endpoint(self):
        """The relative term is what keeps a tiny MIN beside a huge MAX honest.

        ``eps = max(1e-9, 1e-6 * 1e4) = 1e-2``, so a MIN of 1e-4 is NOT above
        eps and the reaction is ``functional``.  An absolute-only tolerance
        would call it ``essential-forward`` on numerical noise — this case is
        the difference between the two rules, and it is why the 1e-6 relative
        term is ported rather than simplified away.
        """
        assert fva_panel.classify_fva_essentiality(1e-4, 1e4) == "functional"

    def test_every_class_is_in_the_valid_set(self):
        for lo, hi in [(0.0, 0.0), (1.0, 2.0), (-2.0, -1.0), (-1.0, 1.0)]:
            assert (
                fva_panel.classify_fva_essentiality(lo, hi)
                in fva_panel.VALID_ESSENTIALITY_CLASSES
            )


class TestComputeFvaEssentiality:
    @pytest.mark.parametrize(
        "fva_min,fva_max,cls,essential,blocked",
        [
            (0.0, 0.0, "blocked", False, True),
            (1.0, 10.0, "essential-forward", True, False),
            (-10.0, -1.0, "essential-reverse", True, False),
            (-5.0, 5.0, "functional", False, False),
        ],
    )
    def test_three_outputs_agree(self, fva_min, fva_max, cls, essential, blocked):
        out = fva_panel.compute_fva_essentiality(fva_min, fva_max)
        assert out == {
            "essentiality_class": cls,
            "is_essential": essential,
            "is_blocked": blocked,
        }

    def test_booleans_are_derived_from_the_class(self):
        """The forward/reverse distinction survives in the class.

        Both essential directions set ``is_essential``, so the two booleans
        alone cannot tell them apart — the class is the authoritative column
        and that is exactly the collapse this shape exists to avoid.
        """
        fwd = fva_panel.compute_fva_essentiality(1.0, 2.0)
        rev = fva_panel.compute_fva_essentiality(-2.0, -1.0)
        assert fwd["is_essential"] is rev["is_essential"] is True
        assert fwd["essentiality_class"] != rev["essentiality_class"]


# ---------------------------------------------------------------------------
# run_fva_panel
# ---------------------------------------------------------------------------


class TestRunFvaPanelGrowthGate:
    def test_growing_media_get_fva_and_non_growing_get_empty(
        self, stub_modelseedpy, cobra_json
    ):
        fba = FakeFBA(
            growth={"A": 1.25, "B": 0.0, "C": 3.0},
            fva={"A": _rows(rxn1=(0.0, 5.0)), "C": _rows(rxn2=(-1.0, 1.0))},
        )
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A", "B", "C"), fba=fba)

        assert out["A"] == {"rxn1": {"MIN": 0.0, "MAX": 5.0}}
        assert out["B"] == {}
        assert out["C"] == {"rxn2": {"MIN": -1.0, "MAX": 1.0}}
        # The non-growing medium was never handed to FVA at all.
        assert [call[0] for call in fba.fva_calls] == ["A", "C"]

    def test_growth_exactly_at_the_threshold_does_not_count_as_growth(
        self, stub_modelseedpy, cobra_json
    ):
        """The gate is ``<=``, so GROWTH_THRESHOLD itself is not growth."""
        fba = FakeFBA(growth={"A": fva_panel.GROWTH_THRESHOLD}, fva={})
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba)
        assert out == {"A": {}}
        assert fba.fva_calls == []

    def test_growth_just_above_the_threshold_does(self, stub_modelseedpy, cobra_json):
        fba = FakeFBA(
            growth={"A": fva_panel.GROWTH_THRESHOLD * 10},
            fva={"A": _rows(rxn1=(1.0, 1.0))},
        )
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba)
        assert out["A"] == {"rxn1": {"MIN": 1.0, "MAX": 1.0}}

    def test_gate_uses_run_pfba_false(self, stub_modelseedpy, cobra_json):
        """A parsimonious distribution costs a second solve nothing here reads."""
        fba = FakeFBA(growth={"A": 1.0}, fva={"A": {}})
        fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba)
        assert fba.fba_calls == [("A", False)]

    @pytest.mark.parametrize(
        "growth_value", ["no-solution", "none-objective", "uncoercible"]
    )
    def test_unmeasurable_growth_degrades_to_no_growth(
        self, stub_modelseedpy, cobra_json, growth_value
    ):
        """A ``None`` solution, a ``None`` objective and a non-numeric objective
        all mean "this medium does not get FVA" — none of them fails the panel."""
        fba = FakeFBA(growth={"A": growth_value}, fva={})
        growth_out: Dict[str, float] = {}
        out = fva_panel.run_fva_panel(
            FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba, growth_out=growth_out
        )
        assert out == {"A": {}}
        assert growth_out == {"A": 0.0}

    def test_raising_gate_degrades_to_no_growth(self, stub_modelseedpy, cobra_json):
        fba = FakeFBA(
            growth={"A": 1.0, "B": 2.0},
            fva={"B": _rows(rxn1=(0.0, 1.0))},
            fba_raises=("A",),
        )
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A", "B"), fba=fba)
        assert out["A"] == {}
        # ...and the panel carried on to the next medium.
        assert out["B"] == {"rxn1": {"MIN": 0.0, "MAX": 1.0}}


class TestRunFvaPanelGrowthOut:
    def test_records_every_medium_growing_and_not(self, stub_modelseedpy, cobra_json):
        """The whole point of the out-parameter: a measured 0.0 is a result.

        A caller that sourced growth from gap-fill records could not tell a
        medium that did not grow from one that grew so well it needed no
        gap-filling — both read 0.0.  Here they are 0.0 and 1.25.
        """
        fba = FakeFBA(growth={"A": 1.25, "B": 0.0}, fva={"A": {}})
        growth_out: Dict[str, float] = {}
        fva_panel.run_fva_panel(
            FakeMdlUtl(FakeModel("m")), _panel("A", "B"), fba=fba, growth_out=growth_out
        )
        assert growth_out == {"A": 1.25, "B": 0.0}

    def test_absent_key_means_never_evaluated(self, stub_modelseedpy, cobra_json):
        fba = FakeFBA(growth={"A": 1.0}, fva={"A": {}})
        growth_out: Dict[str, float] = {}
        fva_panel.run_fva_panel(
            FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba, growth_out=growth_out
        )
        assert "B" not in growth_out

    def test_omitting_growth_out_changes_nothing_else(self, stub_modelseedpy, cobra_json):
        fba = FakeFBA(growth={"A": 1.0}, fva={"A": _rows(rxn1=(0.0, 1.0))})
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba)
        assert out == {"A": {"rxn1": {"MIN": 0.0, "MAX": 1.0}}}


class TestRunFvaPanelModelIsolation:
    def test_snapshot_is_taken_once_before_any_gate_runs(
        self, stub_modelseedpy, cobra_json
    ):
        """Every medium's FVA sees an UNMUTATED model.

        The gate mutates the shared ``mdlutl`` in place (``mutations`` here
        stands in for media bounds and objective constraints).  If the snapshot
        were taken inside the loop, medium B's FVA would run against the model
        A's gate left behind.  Snapshot count is 1 and every FVA model carries
        an empty mutation list — including the last one, by which point the
        shared model has been mutated three times.
        """
        shared = FakeModel("m")
        fba = FakeFBA(
            growth={"A": 1.0, "B": 2.0, "C": 3.0},
            fva={"A": {}, "B": {}, "C": {}},
        )
        fva_panel.run_fva_panel(FakeMdlUtl(shared), _panel("A", "B", "C"), fba=fba)

        assert cobra_json.snapshot_count == 1
        assert [mutations for _, _, mutations, _ in fba.fva_calls] == [[], [], []]
        # The shared model really was mutated — the assertion above is not vacuous.
        assert shared.mutations == ["A", "B", "C"]

    def test_each_medium_gets_a_fresh_copy(self, stub_modelseedpy, cobra_json):
        """Rebuilt from the snapshot AND copied: ``set_media`` attaches its
        package in place and glpk carries basis state between solves."""
        fba = FakeFBA(growth={"A": 1.0, "B": 1.0}, fva={"A": {}, "B": {}})
        fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A", "B"), fba=fba)
        assert [copies for _, _, _, copies in fba.fva_calls] == [1, 1]


class TestRunFvaPanelFractionOfOptimum:
    def test_default_is_the_panel_default_not_run_fva_s_own(
        self, stub_modelseedpy, cobra_json
    ):
        """1.0, not ``run_fva``'s 0.9.

        A caller that leaves this alone is asking for FVA at the optimum, which
        is what the consumers of these numbers assume; inheriting ``run_fva``'s
        0.9 would silently widen every range.
        """
        assert fva_panel.DEFAULT_FRACTION_OF_OPTIMUM == 1.0
        fba = FakeFBA(growth={"A": 1.0}, fva={"A": {}})
        fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba)
        assert fba.fva_calls[0][1] == 1.0

    def test_explicit_value_is_forwarded(self, stub_modelseedpy, cobra_json):
        fba = FakeFBA(growth={"A": 1.0}, fva={"A": {}})
        fva_panel.run_fva_panel(
            FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba, fraction_of_optimum=0.2
        )
        assert fba.fva_calls[0][1] == 0.2

    def test_fraction_of_optimum_is_keyword_only(self):
        import inspect

        params = inspect.signature(fva_panel.run_fva_panel).parameters
        for name in ("fraction_of_optimum", "fba", "growth_out"):
            assert params[name].kind is inspect.Parameter.KEYWORD_ONLY


class TestRunFvaPanelResultShapes:
    def test_empty_panel_is_not_an_error(self, stub_modelseedpy, cobra_json):
        fba = FakeFBA(growth={}, fva={})
        assert fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel(), fba=fba) == {}

    def test_raising_fva_yields_empty_and_the_panel_continues(
        self, stub_modelseedpy, cobra_json
    ):
        fba = FakeFBA(
            growth={"A": 1.0, "B": 1.0},
            fva={"B": _rows(rxn1=(0.0, 1.0))},
            fva_raises=("A",),
        )
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A", "B"), fba=fba)
        assert out["A"] == {}
        assert out["B"] == {"rxn1": {"MIN": 0.0, "MAX": 1.0}}

    def test_endpoints_are_coerced_to_float(self, stub_modelseedpy, cobra_json):
        fba = FakeFBA(growth={"A": 1.0}, fva={"A": {"rxn1": {"MIN": 0, "MAX": 5}}})
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba)
        assert out["A"] == {"rxn1": {"MIN": 0.0, "MAX": 5.0}}
        assert isinstance(out["A"]["rxn1"]["MIN"], float)

    def test_unusable_rows_are_dropped_not_carried_as_none(
        self, stub_modelseedpy, cobra_json
    ):
        """A consumer reading MIN/MAX off every row should not have to defend
        against a row that has neither."""
        fba = FakeFBA(
            growth={"A": 1.0},
            fva={
                "A": {
                    "good": {"MIN": 0.0, "MAX": 1.0},
                    "missing_max": {"MIN": 0.0},
                    "uncoercible": {"MIN": "x", "MAX": "y"},
                    "not_a_dict": 7,
                }
            },
        )
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba)
        assert out["A"] == {"good": {"MIN": 0.0, "MAX": 1.0}}

    def test_non_dict_fva_return_yields_empty(self, stub_modelseedpy, cobra_json):
        fba = FakeFBA(growth={"A": 1.0}, fva={"A": None})
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A"), fba=fba)
        assert out["A"] == {}

    def test_duplicate_media_id_keeps_the_last_evaluation(
        self, stub_modelseedpy, cobra_json
    ):
        """The return is keyed by media id, so a repeated id cannot hold two
        results — recorded here so no caller reads a count off it."""
        fba = FakeFBA(growth={"A": 1.0}, fva={"A": _rows(rxn1=(0.0, 1.0))})
        out = fva_panel.run_fva_panel(FakeMdlUtl(FakeModel("m")), _panel("A", "A"), fba=fba)
        assert list(out) == ["A"]
        assert len(fba.fva_calls) == 2


class TestNoGaaImport:
    def test_the_module_never_imports_genome_annotation_aggregator(self):
        """The architectural point of this task, asserted on the source.

        A KBase CTS job must build and analyse a model with KBUtilLib plus
        ModelSEEDpy alone.  Asserted on IMPORTS at any depth, not on raw text:
        the module docstring names GAA deliberately, to record what this layer
        replaces and where the ported behaviour came from, and a text match
        cannot tell that provenance note from a dependency.
        """
        assert not any(
            name.startswith("genome_annotation_aggregator")
            for name in _all_imports(fva_panel)
        )

    def test_timeout_sentinel_is_still_exported_for_consumers(self):
        """Consumers already branch on it; the sequential path never emits it
        (see the module docstring), but the constant must stay importable."""
        assert fva_panel.TIMEOUT_SENTINEL == {"timeout": True}


class TestEnsureObjectivePackage:
    """The fork/upstream ``ObjectivePkg`` registry divergence.

    ``run_fva`` sets a per-reaction objective via
    ``pkgmgr.getpkg("ObjectivePkg")``, and ``MSPackageManager`` builds its
    registry from the names exported at the TOP LEVEL of ``modelseedpy``.  The
    fork re-exports ``ObjectivePkg`` there; upstream dev does not, though the
    class is importable from ``modelseedpy.fbapkg.objectivepkg`` in both.
    Without the shim, every reaction raises ``PackageError`` on upstream and a
    whole medium's FVA comes back empty — measured, and the reason the real
    parity run initially produced 0 rows for both media.
    """

    @pytest.fixture
    def stub_objectivepkg(self, monkeypatch):
        """Make the class importable regardless of which venv is running.

        The task venv has no modelseedpy at all and the scratch venv has
        upstream's; stubbing keeps this test about the shim's logic rather than
        about which packages happen to be installed.
        """
        module = types.ModuleType("modelseedpy.fbapkg.objectivepkg")

        class _ObjectivePkg:
            pass

        module.ObjectivePkg = _ObjectivePkg
        fbapkg = types.ModuleType("modelseedpy.fbapkg")
        fbapkg.objectivepkg = module
        monkeypatch.setitem(sys.modules, "modelseedpy.fbapkg", fbapkg)
        monkeypatch.setitem(sys.modules, "modelseedpy.fbapkg.objectivepkg", module)
        return _ObjectivePkg

    class _PkgMgr:
        def __init__(self, available):
            self.available_packages = available

    class _Utl:
        def __init__(self, pkgmgr):
            self.pkgmgr = pkgmgr

    def test_registers_the_class_when_the_registry_lacks_it(self, stub_objectivepkg):
        """The upstream case."""
        available = {"KBaseMediaPkg": object()}
        utl = self._Utl(self._PkgMgr(available))
        fva_panel._ensure_objective_package(mdlutl=utl)
        assert available["ObjectivePkg"] is stub_objectivepkg

    def test_is_a_noop_when_the_registry_already_has_it(self, stub_objectivepkg):
        """The fork case — nothing is replaced, so behaviour there is unchanged."""
        sentinel = object()
        available = {"ObjectivePkg": sentinel}
        utl = self._Utl(self._PkgMgr(available))
        fva_panel._ensure_objective_package(mdlutl=utl)
        assert available["ObjectivePkg"] is sentinel

    def test_leaves_other_registry_entries_alone(self, stub_objectivepkg):
        media_pkg = object()
        available = {"KBaseMediaPkg": media_pkg}
        utl = self._Utl(self._PkgMgr(available))
        fva_panel._ensure_objective_package(mdlutl=utl)
        assert available["KBaseMediaPkg"] is media_pkg
        assert set(available) == {"KBaseMediaPkg", "ObjectivePkg"}

    def test_touches_only_this_models_registry(self, stub_objectivepkg):
        """``MSPackageManager`` keeps one manager per model, so the fix is local.

        Patching a per-instance dict is what keeps this from being a global
        monkeypatch of modelseedpy.
        """
        mine = {}
        other = {}
        fva_panel._ensure_objective_package(mdlutl=self._Utl(self._PkgMgr(mine)))
        assert "ObjectivePkg" in mine
        assert other == {}

    def test_tolerates_a_model_with_no_package_manager(self, stub_objectivepkg):
        """Test doubles need not implement ``pkgmgr``; run_fva will report any
        real problem itself rather than this helper failing the panel first."""

        class _Bare:
            pass

        fva_panel._ensure_objective_package(mdlutl=_Bare())  # must not raise

    def test_tolerates_a_package_manager_with_no_registry(self, stub_objectivepkg):
        class _OddMgr:
            pass

        fva_panel._ensure_objective_package(mdlutl=self._Utl(_OddMgr()))  # must not raise

    def test_absent_objectivepkg_module_is_not_fatal(self, monkeypatch):
        """If upstream moves the class, run_fva reports it — not this helper."""
        monkeypatch.setitem(sys.modules, "modelseedpy.fbapkg.objectivepkg", None)
        available: Dict[str, Any] = {}
        fva_panel._ensure_objective_package(mdlutl=self._Utl(self._PkgMgr(available)))
        assert available == {}
