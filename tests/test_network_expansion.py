"""Tests for the network-expansion facade and its cheminformatics backends.

These tests run without any optional scientific dependency installed. The
success path is exercised with an injected fake backend so neither RDKit, the
~500 MB RetroRules dump, nor a minedatabase install is needed in CI; the
graceful-degradation path is exercised by the real backends, which are expected
to report ``available == False`` when their dependency/data is absent.

Optional *live* integration tests against the real RetroRules TSV / Pickaxe
rule data are included but skipped unless the relevant env var points at the
data, so a developer with the data on disk can opt in.

Core invariants under test:
  * Importing the subpackage never requires an optional dependency.
  * Backends never raise at construction when their dependency is absent.
  * Backends never fabricate a structure or reaction: nothing -> empty result
    + warnings.
  * The dispatcher walks the priority order and falls through cleanly.
  * The toolkit exposes the ``network_expansion`` lazy property.
"""

from __future__ import annotations

import os

import pytest

from kbutillib.cheminformatics import (
    ExpansionResult,
    PickaxeBackend,
    PredictedCompound,
    PredictedReaction,
    RetroRulesBackend,
)
from kbutillib.cheminformatics.base import BackendUnavailableError, ExpansionBackend
from kbutillib.network_expansion_utils import (
    DEFAULT_EXPANSION_ORDER,
    NetworkExpansionUtils,
    NetworkExpansionUtilsImpl,
)

# ── fakes ───────────────────────────────────────────────────────────────


class _FakeBackend:
    """Minimal in-memory ExpansionBackend producing one canned reaction."""

    def __init__(self, name="fake", available=True, expanded=True):
        self.name = name
        self._available = available
        self._expanded = expanded

    @property
    def available(self):
        return self._available

    @property
    def unavailable_reason(self):
        return None if self._available else f"{self.name} not configured"

    @property
    def capabilities(self):
        return frozenset({"expand"})

    def expand(self, seed_smiles, generations=1, **kwargs):
        if not self._available:
            raise BackendUnavailableError(self.unavailable_reason)
        res = ExpansionResult(backend=self.name, generations=generations)
        for cid, smi in seed_smiles.items():
            res.compounds[cid] = PredictedCompound(
                compound_id=cid, smiles=smi, generation=0, is_seed=True
            )
        if self._expanded and seed_smiles:
            res.compounds["P1"] = PredictedCompound(
                compound_id="P1", smiles="CCO", generation=1, is_seed=False
            )
            res.reactions.append(
                PredictedReaction(
                    reaction_id="R1",
                    backend=self.name,
                    operator="op1",
                    reactant_ids=list(seed_smiles.keys()),
                    product_ids=["P1"],
                    generation=1,
                )
            )
        return res


def _make_utils(**backends):
    u = NetworkExpansionUtils(
        config_file=False, token_file=None, kbase_token_file=None
    )
    u._backends = dict(backends)
    return u


# ── import / construction invariants ────────────────────────────────────


def test_import_does_not_require_optional_deps():
    import importlib

    mod = importlib.import_module("kbutillib.cheminformatics")
    assert hasattr(mod, "PickaxeBackend")
    assert hasattr(mod, "RetroRulesBackend")


def test_backends_construct_without_deps():
    # Construction must never raise even if minedatabase / rdkit / data absent.
    p = PickaxeBackend()
    r = RetroRulesBackend()
    assert isinstance(p.available, bool)
    assert isinstance(r.available, bool)


def test_backends_are_structural_expansion_backends():
    assert isinstance(PickaxeBackend(), ExpansionBackend)
    assert isinstance(RetroRulesBackend(), ExpansionBackend)


def test_unavailable_backend_reports_reason_not_raise():
    # With no data dir configured, pickaxe is unavailable with a helpful reason.
    p = PickaxeBackend(config_resolver=lambda key, default=None: None)
    if not p.available:
        assert p.unavailable_reason
        with pytest.raises(BackendUnavailableError):
            p.expand({"c1": "CCO"})


def test_retrorules_unavailable_without_tsv(monkeypatch):
    monkeypatch.delenv("KBUTILLIB_RETRORULES_TSV", raising=False)
    r = RetroRulesBackend(config_resolver=lambda key, default=None: None)
    # Either rdkit is missing or the TSV is missing; either way: unavailable.
    if not r.available:
        assert r.unavailable_reason
        with pytest.raises(BackendUnavailableError):
            r.expand({"c1": "CCO"})


# ── dataclass invariants ────────────────────────────────────────────────


def test_expansion_result_helpers():
    res = ExpansionResult(backend="x")
    assert res.n_compounds == 0
    assert res.n_reactions == 0
    assert res.is_expanded is False
    res.compounds["s"] = PredictedCompound("s", smiles="C", is_seed=True)
    res.compounds["p"] = PredictedCompound("p", smiles="CC", is_seed=False)
    res.reactions.append(PredictedReaction("r", backend="x", product_ids=["p"]))
    assert res.n_compounds == 2
    assert res.n_reactions == 1
    assert res.is_expanded is True
    assert [c.compound_id for c in res.product_compounds()] == ["p"]
    d = res.to_dict()
    assert d["backend"] == "x"
    assert set(d["compounds"]) == {"s", "p"}
    assert d["reactions"][0]["reaction_id"] == "r"


# ── dispatcher: success path with injected fake ─────────────────────────


def test_dispatch_success_with_fake_backend():
    u = _make_utils(fake=_FakeBackend(name="fake"))
    res = u.expand({"c1": "CC=O"}, generations=1, backend="fake")
    assert res.is_expanded
    assert res.backend == "fake"
    assert res.n_reactions == 1
    assert any(not c.is_seed for c in res.compounds.values())


def test_dispatch_walks_priority_order():
    # First backend unavailable, second produces the expansion.
    u = _make_utils(
        a=_FakeBackend(name="a", available=False),
        b=_FakeBackend(name="b", available=True),
    )
    assert u._resolve_order(None, ["a", "b"]) == ["a", "b"]
    import kbutillib.network_expansion_utils as nx

    original = nx.DEFAULT_EXPANSION_ORDER
    try:
        nx.DEFAULT_EXPANSION_ORDER = ("a", "b")
        out = u.expand({"c1": "CCO"}, backend=None)
    finally:
        nx.DEFAULT_EXPANSION_ORDER = original
    assert out.is_expanded and out.backend == "b"


def test_dispatch_all_unavailable_returns_empty_not_fabricated():
    u = _make_utils(
        a=_FakeBackend(name="a", available=False),
        b=_FakeBackend(name="b", available=False),
    )
    res = u.expand({"c1": "CCO"}, backend="a")
    assert not res.is_expanded
    assert res.n_reactions == 0
    assert res.warnings  # explains why


def test_dispatch_empty_expansion_falls_through():
    # 'empty' is available but yields no reactions; 'good' then succeeds.
    u = _make_utils(
        empty=_FakeBackend(name="empty", available=True, expanded=False),
        good=_FakeBackend(name="good", available=True, expanded=True),
    )
    u_order = ["empty", "good"]
    # Use the internal loop by forcing default order through a subclass-free shim
    import kbutillib.network_expansion_utils as nx

    original = nx.DEFAULT_EXPANSION_ORDER
    try:
        nx.DEFAULT_EXPANSION_ORDER = tuple(u_order)
        res = u.expand({"c1": "CCO"}, backend=None)
    finally:
        nx.DEFAULT_EXPANSION_ORDER = original
    assert res.is_expanded
    assert res.backend == "good"


def test_unknown_backend_name_raises_keyerror():
    u = _make_utils(fake=_FakeBackend())
    with pytest.raises(KeyError):
        u.get_backend("nope")


def test_backend_status_shape():
    u = _make_utils(
        a=_FakeBackend(name="a", available=True),
        b=_FakeBackend(name="b", available=False),
    )
    st = u.backend_status()
    assert st["a"]["available"] is True
    assert st["b"]["available"] is False
    assert st["b"]["reason"]
    assert "expand" in st["a"]["capabilities"]


def test_default_order_constant():
    assert DEFAULT_EXPANSION_ORDER == ("pickaxe", "retrorules")


# ── toolkit integration ─────────────────────────────────────────────────


def test_toolkit_exposes_network_expansion():
    from kbutillib.toolkit import KBUtilLib

    tk = KBUtilLib(config_file=False, token_file=None, kbase_token_file=None)
    ne = tk.network_expansion
    assert isinstance(ne, NetworkExpansionUtilsImpl)
    assert hasattr(ne, "expand")
    # idempotent / cached
    assert tk.network_expansion is ne


# ── optional live integration (skipped unless data present) ─────────────

_RR_TSV = os.environ.get("KBUTILLIB_RETRORULES_TSV")
_PICKAXE_DATA = os.environ.get("KBUTILLIB_PICKAXE_DATA_DIR")


@pytest.mark.skipif(
    not (_RR_TSV and os.path.isfile(_RR_TSV)),
    reason="RetroRules TSV not configured (set KBUTILLIB_RETRORULES_TSV)",
)
def test_retrorules_live_expansion():
    r = RetroRulesBackend()
    if not r.available:
        pytest.skip(r.unavailable_reason or "rdkit missing")
    res = r.expand({"propanal": "CCC=O"}, generations=1, diameter=2, max_rules=2000)
    assert res.n_compounds >= 1
    # If any rule fired, every product must be a real (parseable) SMILES.
    for c in res.product_compounds():
        assert c.smiles


@pytest.mark.skipif(
    not (_PICKAXE_DATA and os.path.isdir(_PICKAXE_DATA)),
    reason="Pickaxe rule data not configured (set KBUTILLIB_PICKAXE_DATA_DIR)",
)
def test_pickaxe_live_expansion():
    p = PickaxeBackend()
    if not p.available:
        pytest.skip(p.unavailable_reason or "minedatabase missing")
    res = p.expand({"glucose": "OCC1OC(O)C(O)C(O)C1O"}, generations=1)
    assert res.n_compounds >= 1
    for c in res.product_compounds():
        assert c.smiles
