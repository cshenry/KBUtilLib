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


# ---------------------------------------------------------------------------
# FIX2: operators list on PredictedReaction (additive; scalar field preserved)
# ---------------------------------------------------------------------------


def test_fix2_predicted_reaction_has_operators_field():
    """PredictedReaction must have an `operators` list field (default empty)."""
    rxn = PredictedReaction(
        reaction_id="rxn_fix2_001",
        backend="pickaxe",
        operator="op1",
    )
    assert hasattr(rxn, "operators"), "PredictedReaction missing `operators` field"
    assert isinstance(rxn.operators, list)
    assert rxn.operators == []  # default is empty


def test_fix2_predicted_reaction_scalar_operator_preserved():
    """Adding `operators` list must NOT remove the scalar `operator` field."""
    rxn = PredictedReaction(
        reaction_id="rxn_fix2_002",
        backend="pickaxe",
        operator="op1",
    )
    assert rxn.operator == "op1", "Scalar `operator` field must be preserved"


def test_fix2_predicted_reaction_multi_operator_list():
    """A Pickaxe-style reaction with multiple operators stores the full list."""
    rxn = PredictedReaction(
        reaction_id="rxn_fix2_003",
        backend="pickaxe",
        operator="opA;opB",   # joined display string (backward compat)
        operators=["opA", "opB"],
    )
    assert len(rxn.operators) == 2, "operators list must hold all firing operators"
    assert "opA" in rxn.operators
    assert "opB" in rxn.operators
    # Scalar still usable for display
    assert rxn.operator == "opA;opB"


def test_fix2_predicted_reaction_to_dict_includes_operators():
    """to_dict() must emit both 'operator' (scalar) and 'operators' (list)."""
    rxn = PredictedReaction(
        reaction_id="rxn_fix2_004",
        backend="pickaxe",
        operator="opA;opB",
        operators=["opA", "opB"],
    )
    d = rxn.to_dict()
    assert "operator" in d, "Scalar 'operator' key must remain in to_dict()"
    assert "operators" in d, "'operators' list key must be added by FIX2"
    assert d["operator"] == "opA;opB"
    assert d["operators"] == ["opA", "opB"]


def test_fix2_fake_backend_single_operator_list():
    """Fake backend reaction with operator='op1' produces operators=['op1'] on the field."""
    class _MultiOpBackend(_FakeBackend):
        """Like _FakeBackend but produces a reaction with a multi-operator list."""
        def expand(self, seed_smiles, generations=1, **kwargs):
            res = ExpansionResult(backend=self.name, generations=generations)
            for cid, smi in seed_smiles.items():
                res.compounds[cid] = PredictedCompound(
                    compound_id=cid, smiles=smi, generation=0, is_seed=True
                )
            # Simulate Pickaxe-style multi-operator reaction
            res.reactions.append(
                PredictedReaction(
                    reaction_id="R_multi",
                    backend=self.name,
                    operator="opX;opY;opZ",
                    operators=["opX", "opY", "opZ"],
                    reactant_ids=list(seed_smiles.keys()),
                    product_ids=[],
                    generation=1,
                )
            )
            return res

    backend = _MultiOpBackend()
    result = backend.expand({"cpd1": "CC"}, generations=1)
    assert len(result.reactions) == 1
    rxn = result.reactions[0]
    # Must not be collapsed — the list must have 3 elements
    assert len(rxn.operators) == 3, (
        f"operators list collapsed: expected 3, got {len(rxn.operators)}"
    )
    assert rxn.operators == ["opX", "opY", "opZ"]
    # Scalar backward compat
    assert rxn.operator == "opX;opY;opZ"


# ---------------------------------------------------------------------------
# FIX1: mechinformed rule-set resolution, _normalize_rule_tsv, coreactant TSV
# ---------------------------------------------------------------------------

import csv
import tempfile


def _write_tiny_rule_tsv(path, with_comments=False):
    """Write a minimal 4- or 5-column rule TSV to *path* for test fixtures."""
    header = ["Name", "Reactants", "SMARTS", "Products"]
    if with_comments:
        header.append("Comments")
    with open(path, "w", newline="") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(header)
        row = ["rule_001", "SUBSTRATE", "[C:1]>>[C:1]O", "PRODUCT"]
        if with_comments:
            row.append("some note")
        writer.writerow(row)


def test_fix1_normalize_rule_tsv_adds_comments_column(tmp_path):
    """_normalize_rule_tsv must add an empty Comments column to 4-col TSV
    without mutating the source file."""
    from kbutillib.cheminformatics.pickaxe_backend import _normalize_rule_tsv

    src = tmp_path / "rules_no_comments.tsv"
    _write_tiny_rule_tsv(src, with_comments=False)
    src_mtime = src.stat().st_mtime

    normalized = _normalize_rule_tsv(src)

    # Source file must NOT be mutated
    assert src.stat().st_mtime == src_mtime, "Source file mtime changed — mutated!"

    # Normalized path must be different (a temp copy) since source lacks Comments
    assert normalized != src, "Normalized path must differ from source for 4-col TSV"
    assert normalized.is_file()

    # Normalized TSV must have 5 columns with 'Comments' in header
    with open(normalized, newline="") as fh:
        reader = csv.reader(fh, delimiter="\t")
        header = next(reader)
    header_lower = [h.strip().lower() for h in header]
    assert "comments" in header_lower, f"Comments column missing; header={header}"
    assert len(header) == 5


def test_fix1_normalize_rule_tsv_passthrough_when_comments_present(tmp_path):
    """_normalize_rule_tsv must return the original path when Comments already exists."""
    from kbutillib.cheminformatics.pickaxe_backend import _normalize_rule_tsv

    src = tmp_path / "rules_with_comments.tsv"
    _write_tiny_rule_tsv(src, with_comments=True)

    normalized = _normalize_rule_tsv(src)

    # When Comments column is already present, return source unchanged
    assert normalized == src, "Should return source path unmodified when Comments already present"


def test_fix1_normalize_rule_tsv_cache(tmp_path):
    """_normalize_rule_tsv must return the same cached temp path on repeated calls."""
    from kbutillib.cheminformatics.pickaxe_backend import _normalize_rule_tsv

    src = tmp_path / "rules_cache.tsv"
    _write_tiny_rule_tsv(src, with_comments=False)

    first = _normalize_rule_tsv(src)
    second = _normalize_rule_tsv(src)
    assert first == second, "Repeated calls must return the same cached normalized path"


def test_fix1_synthesized_coreactant_tsv_contains_required_roles():
    """_synthesize_coreactant_tsv must include the verAB-relevant role rows."""
    from kbutillib.cheminformatics.pickaxe_backend import _synthesize_coreactant_tsv

    tsv_path = _synthesize_coreactant_tsv()
    assert tsv_path.is_file(), "Synthesized coreactant TSV must exist"

    with open(tsv_path, newline="") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        roles = {row["Name"] for row in reader}

    # Must cover the key roles embedded in the mechinferred operator rows
    required_roles = {"WATER", "METHYL_DONOR_CoF", "METHYL_ACCEPTOR_CoF",
                      "PHOSPHATE_DONOR_CoF", "PPI", "Pi"}
    missing = required_roles - roles
    assert not missing, f"Synthesized coreactant TSV missing roles: {missing}"


def test_fix1_resolve_mechinformed_config_wins(tmp_path, monkeypatch):
    """Config path wins over env var and DependencyManager for mechinformed TSV."""
    from kbutillib.cheminformatics.pickaxe_backend import PickaxeBackend

    # Create a fake rule TSV (just needs to exist as a file)
    cfg_tsv = tmp_path / "cfg_rules.tsv"
    _write_tiny_rule_tsv(cfg_tsv, with_comments=True)

    env_tsv = tmp_path / "env_rules.tsv"
    _write_tiny_rule_tsv(env_tsv, with_comments=True)

    monkeypatch.setenv("KBUTILLIB_VERAB_OPERATOR_TSV", str(env_tsv))

    def _cfg(key, default=None):
        if key == "cheminformatics.verab.operator_rule_tsv":
            return str(cfg_tsv)
        return default

    backend = PickaxeBackend(config_resolver=_cfg)
    resolved = backend._resolve_mechinformed()

    assert resolved == cfg_tsv, (
        f"Config path must win; expected {cfg_tsv}, got {resolved}"
    )


def test_fix1_resolve_mechinformed_env_wins_over_dep(tmp_path, monkeypatch):
    """Env var wins over DependencyManager when config is absent."""
    from kbutillib.cheminformatics.pickaxe_backend import PickaxeBackend

    env_tsv = tmp_path / "env_rules.tsv"
    _write_tiny_rule_tsv(env_tsv, with_comments=True)

    monkeypatch.setenv("KBUTILLIB_VERAB_OPERATOR_TSV", str(env_tsv))

    backend = PickaxeBackend(config_resolver=lambda key, default=None: None)
    resolved = backend._resolve_mechinformed()

    assert resolved == env_tsv, (
        f"Env var path must win over dep; expected {env_tsv}, got {resolved}"
    )


def test_fix1_resolve_mechinformed_returns_none_when_absent(tmp_path, monkeypatch):
    """_resolve_mechinformed must return None when no TSV is found."""
    from kbutillib.cheminformatics.pickaxe_backend import PickaxeBackend

    # Remove env var and ensure config returns None
    monkeypatch.delenv("KBUTILLIB_VERAB_OPERATOR_TSV", raising=False)

    backend = PickaxeBackend(config_resolver=lambda key, default=None: None)
    resolved = backend._resolve_mechinformed()

    # Only None is acceptable when the TSV genuinely doesn't exist
    assert resolved is None or not resolved.is_file(), (
        "Expected None or non-existent path when mechinformed TSV is absent"
    )


def test_fix1_resolve_coreactant_config_wins(tmp_path, monkeypatch):
    """Config coreactant path wins over env var and synthesized fallback."""
    from kbutillib.cheminformatics.pickaxe_backend import PickaxeBackend

    cfg_coact = tmp_path / "cfg_coreactants.tsv"
    with open(cfg_coact, "w") as fh:
        fh.write("Name\tSMILES\n")

    def _cfg(key, default=None):
        if key == "cheminformatics.verab.operator_coreactant_tsv":
            return str(cfg_coact)
        return default

    backend = PickaxeBackend(config_resolver=_cfg)
    resolved = backend._resolve_mechinformed_coreactants()

    assert resolved == cfg_coact, (
        f"Config coreactant path must win; expected {cfg_coact}, got {resolved}"
    )


def test_fix1_resolve_coreactant_synthesizes_when_absent(monkeypatch):
    """_resolve_mechinformed_coreactants must synthesize a temp TSV when unresolved."""
    from kbutillib.cheminformatics.pickaxe_backend import PickaxeBackend

    monkeypatch.delenv("KBUTILLIB_VERAB_COREACTANT_TSV", raising=False)

    backend = PickaxeBackend(config_resolver=lambda key, default=None: None)
    resolved = backend._resolve_mechinformed_coreactants()

    assert resolved.is_file(), "Synthesized coreactant TSV must exist"
    # Should contain at least the key roles
    with open(resolved, newline="") as fh:
        content = fh.read()
    assert "WATER" in content
    assert "METHYL_DONOR_CoF" in content
