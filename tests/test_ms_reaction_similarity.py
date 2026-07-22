"""Tests for :mod:`kbutillib.ms_reaction_similarity_utils`.

The chemistry / distance-matrix / clustering logic is exercised against a
``FakeBerdl`` stub that mimics the subset of SQL the module emits, so no network
or KBase token is required. Live end-to-end checks are marked ``kbase`` and run
only when ``KBASE_LIVE_TESTS=1``.
"""

import re

import pytest

import kbutillib
from kbutillib.ms_reaction_similarity_utils import (
    MSReactionSimilarityUtils,
    MSReactionSimilarityUtilsImpl,
    _drfp_tanimoto,
)

try:
    import numpy as np
    _HAVE_NUMPY = True
except ImportError:  # pragma: no cover
    _HAVE_NUMPY = False

try:
    import drfp  # noqa: F401
    from rdkit import Chem  # noqa: F401
    _HAVE_CHEM = True
except ImportError:  # pragma: no cover
    _HAVE_CHEM = False

needs_numpy = pytest.mark.skipif(not _HAVE_NUMPY, reason="numpy not installed")
needs_chem = pytest.mark.skipif(not _HAVE_CHEM, reason="rdkit/drfp not installed")

DB = "kbase_msd_biochemistry"

# ── tiny in-memory biochemistry ──────────────────────────────────────────
SMILES = {
    "seed.compound:cpd00001": "O",
    "seed.compound:cpdA": "CCO",
    "seed.compound:cpdB": "CC=O",
    "seed.compound:cpdC": "CC(=O)O",
    "seed.compound:cpdD": "C",
    "seed.compound:cpdX": "",  # no structure -> unresolvable
}
REAGENTS = {
    "seed.reaction:rxn1": [("seed.compound:cpdA", -1.0), ("seed.compound:cpdB", 1.0)],
    "seed.reaction:rxn2": [("seed.compound:cpdA", -1.0), ("seed.compound:cpdC", 1.0)],
    "seed.reaction:rxn3": [("seed.compound:cpdD", -1.0), ("seed.compound:cpdA", 1.0)],
    "seed.reaction:rxn4": [("seed.compound:cpdA", -1.0), ("seed.compound:cpdA", 1.0)],
    "seed.reaction:rxn5": [("seed.compound:cpdX", -1.0), ("seed.compound:cpdB", 1.0)],
}
TRANSPORT = {"seed.reaction:rxn4"}
SIM = {  # unordered pairs, stored once
    ("seed.reaction:rxn1", "seed.reaction:rxn2"): 0.9,
    ("seed.reaction:rxn1", "seed.reaction:rxn3"): 0.2,
    ("seed.reaction:rxn2", "seed.reaction:rxn3"): 0.25,
    ("seed.reaction:rxn1", "seed.reaction:rxn4"): 0.05,
}


def _ids_in(sql):
    inside = sql[sql.index("IN (") + 4 : sql.index(")", sql.index("IN ("))]
    return re.findall(r"'([^']+)'", inside)


class FakeBerdl:
    """Minimal stand-in for KBBERDLUtilsImpl.query covering the module's SQL."""

    def query(self, sql, limit=None, offset=0, timeout=None):
        rows = self._dispatch(sql)
        if "ORDER BY similarity DESC" in sql:
            rows = sorted(rows, key=lambda r: r["similarity"], reverse=True)
        page = rows[offset : offset + (limit or len(rows))]
        return {"success": True, "data": page, "columns": list(page[0]) if page else [],
                "row_count": len(page), "has_more": offset + len(page) < len(rows)}

    def _dispatch(self, sql):
        if ".reagent" in sql:
            rid = re.search(r"reaction_id = '([^']+)'", sql).group(1)
            return [{"molecule_id": m, "stoichiometry": s} for m, s in REAGENTS.get(rid, [])]
        if ".molecule" in sql:
            return [{"id": i, "smiles": SMILES.get(i, "")} for i in _ids_in(sql) if i in SMILES]
        if ".reaction_similarity" in sql:
            if "reaction_2 AS other" in sql:  # distance per-row
                a = re.search(r"reaction_1 = '([^']+)'", sql).group(1)
                allowed = set(_ids_in(sql))
                out = []
                for (x, y), v in SIM.items():
                    if x == a and y in allowed:
                        out.append({"other": y, "similarity": v})
                return out
            if "AS rid" in sql:  # similar_reactions
                thr = float(re.search(r"similarity >= ([\d.]+)", sql).group(1))
                if "reaction_1 = '" in sql:
                    me = re.search(r"reaction_1 = '([^']+)'", sql).group(1)
                    return [{"rid": y, "similarity": v} for (x, y), v in SIM.items()
                            if x == me and v >= thr]
                me = re.search(r"reaction_2 = '([^']+)'", sql).group(1)
                return [{"rid": x, "similarity": v} for (x, y), v in SIM.items()
                        if y == me and v >= thr]
            if "SELECT similarity" in sql:  # pair lookup
                ids = re.findall(r"reaction_[12]='([^']+)'", sql)
                key = tuple(sorted(set(ids)))
                for (x, y), v in SIM.items():
                    if tuple(sorted((x, y))) == key:
                        return [{"similarity": v}]
                return []
        if ".reaction" in sql and "is_transport = true" in sql:
            return [{"id": i} for i in _ids_in(sql) if i in TRANSPORT]
        raise AssertionError(f"Unhandled SQL: {sql}")  # pragma: no cover


@pytest.fixture
def rs():
    """A reaction-similarity utility backed by the FakeBerdl stub."""
    return MSReactionSimilarityUtils(
        berdl=FakeBerdl(), config_file=False, token_file=None, kbase_token_file=None
    )


# ── exports + plumbing ───────────────────────────────────────────────────

def test_exports_present():
    assert kbutillib.MSReactionSimilarityUtils is not None
    assert kbutillib.MSReactionSimilarityUtilsImpl is not None


def test_validate_id_rejects_injection(rs):
    with pytest.raises(ValueError):
        rs._validate_id("rxn'; DROP TABLE x;--")
    assert rs._validate_id("seed.reaction:rxn00001") == "seed.reaction:rxn00001"


def test_impl_delegates(shared_env):
    impl = MSReactionSimilarityUtilsImpl(shared_env, berdl=FakeBerdl())
    assert impl.env is shared_env
    assert impl.database == DB  # delegated attribute


# ── id -> SMILES ─────────────────────────────────────────────────────────

def test_resolve_smarts_passthrough(rs):
    assert rs.resolve_to_smarts("CCO>>CC=O") == "CCO>>CC=O"


def test_get_reaction_smiles(rs):
    assert rs.get_reaction_smiles("seed.reaction:rxn1") == "CCO>>CC=O"


def test_get_reaction_smiles_unresolvable(rs):
    assert rs.get_reaction_smiles("seed.reaction:rxn5") is None  # cpdX has no SMILES


# ── similar_reactions ────────────────────────────────────────────────────

def test_similar_reactions_sorted_and_filtered(rs):
    out = rs.similar_reactions("seed.reaction:rxn1", min_similarity=0.0, top_k=None)
    ids = [d["reaction_id"] for d in out]
    assert ids == ["seed.reaction:rxn2", "seed.reaction:rxn3", "seed.reaction:rxn4"]
    assert out[0]["similarity"] == pytest.approx(0.9)


def test_similar_reactions_min_and_topk(rs):
    out = rs.similar_reactions("seed.reaction:rxn1", min_similarity=0.1, top_k=1)
    assert [d["reaction_id"] for d in out] == ["seed.reaction:rxn2"]


def test_similar_reactions_excludes_self(rs):
    out = rs.similar_reactions("seed.reaction:rxn2", min_similarity=0.0, top_k=None)
    assert all(d["reaction_id"] != "seed.reaction:rxn2" for d in out)
    # rxn2 connects to rxn1 (0.9) and rxn3 (0.25)
    assert {d["reaction_id"] for d in out} == {"seed.reaction:rxn1", "seed.reaction:rxn3"}


def test_expand_reactions(rs):
    exp = rs.expand_reactions(["seed.reaction:rxn1"], min_similarity=0.5, top_k_per=5)
    assert list(exp) == ["seed.reaction:rxn1"]
    assert [d["reaction_id"] for d in exp["seed.reaction:rxn1"]] == ["seed.reaction:rxn2"]


def test_similarity_berdl(rs):
    assert rs.similarity("seed.reaction:rxn1", "seed.reaction:rxn2") == pytest.approx(0.9)
    assert rs.similarity("seed.reaction:rxn2", "seed.reaction:rxn1") == pytest.approx(0.9)
    assert rs.similarity("seed.reaction:rxn3", "seed.reaction:rxn4") is None  # absent


def test_similarity_bad_method(rs):
    with pytest.raises(ValueError):
        rs.similarity("a", "b", method="nope")


# ── distance matrix ──────────────────────────────────────────────────────

@needs_numpy
def test_distance_matrix_berdl_invariants(rs):
    ids = ["seed.reaction:rxn1", "seed.reaction:rxn2", "seed.reaction:rxn3"]
    D, out_ids, info = rs.distance_matrix(ids, source="berdl")
    assert out_ids == ids
    assert D.shape == (3, 3)
    assert np.allclose(D, D.T)
    assert np.allclose(np.diag(D), 0.0)
    # rxn1-rxn2 sim 0.9 -> distance 0.1
    assert D[0, 1] == pytest.approx(0.1)
    assert info["pairs_present"] == 3
    assert info["coverage"] == pytest.approx(1.0)


@needs_numpy
def test_distance_matrix_berdl_fill_missing(rs):
    ids = ["seed.reaction:rxn1", "seed.reaction:rxn4"]  # no rxn1-rxn4? it exists at 0.05
    ids = ["seed.reaction:rxn3", "seed.reaction:rxn4"]  # this pair is absent
    D, _, info = rs.distance_matrix(ids, source="berdl", fill_missing=0.77)
    assert D[0, 1] == pytest.approx(0.77)
    assert info["pairs_present"] == 0


@needs_chem
def test_distance_matrix_drfp(rs):
    ids = ["seed.reaction:rxn1", "seed.reaction:rxn2", "seed.reaction:rxn5"]
    D, kept, info = rs.distance_matrix(ids, source="drfp")
    assert "seed.reaction:rxn5" in info["dropped"]  # unresolvable
    assert set(kept) == {"seed.reaction:rxn1", "seed.reaction:rxn2"}
    assert D.shape == (2, 2)
    assert np.allclose(np.diag(D), 0.0)


def test_distance_matrix_bad_source(rs):
    with pytest.raises(ValueError):
        rs.distance_matrix(["seed.reaction:rxn1"], source="nope")


# ── clustering ───────────────────────────────────────────────────────────

@needs_numpy
def test_cluster_segregates_transport(rs):
    ids = ["seed.reaction:rxn1", "seed.reaction:rxn2", "seed.reaction:rxn3", "seed.reaction:rxn4"]
    res = rs.cluster(ids, source="berdl", algorithm="agglomerative", distance_threshold=0.3)
    assert "seed.reaction:rxn4" in res["transport"]
    assert "seed.reaction:rxn4" not in res["labels"]
    # rxn1 & rxn2 (distance 0.1) should land together at threshold 0.3
    assert res["labels"]["seed.reaction:rxn1"] == res["labels"]["seed.reaction:rxn2"]
    assert res["n_clusters"] >= 1
    assert set(res["representatives"].values()) <= set(ids)


@needs_numpy
def test_cluster_butina(rs):
    pytest.importorskip("rdkit")
    ids = ["seed.reaction:rxn1", "seed.reaction:rxn2", "seed.reaction:rxn3"]
    res = rs.cluster(ids, source="berdl", algorithm="butina",
                     distance_threshold=0.3, segregate_transport=False)
    assert res["labels"]["seed.reaction:rxn1"] == res["labels"]["seed.reaction:rxn2"]


@needs_numpy
def test_cluster_bad_algorithm(rs):
    with pytest.raises(ValueError):
        rs.cluster(["seed.reaction:rxn1", "seed.reaction:rxn2"],
                   source="berdl", algorithm="nope", segregate_transport=False)


# ── helpers ──────────────────────────────────────────────────────────────

@needs_numpy
def test_drfp_tanimoto_edge_cases():
    empty = np.zeros(8, dtype=bool)
    a = np.array([1, 1, 0, 0, 0, 0, 0, 0], dtype=bool)
    b = np.array([1, 0, 1, 0, 0, 0, 0, 0], dtype=bool)
    assert _drfp_tanimoto(empty, empty) == 1.0       # both identity reactions
    assert _drfp_tanimoto(empty, a) == 0.0           # one identity, one not
    assert _drfp_tanimoto(a, b) == pytest.approx(1 / 3)


# ── SMARTS entry (recompute path) ────────────────────────────────────────

@needs_chem
def test_similar_to_smarts_ranks_by_recompute(rs):
    cands = ["seed.reaction:rxn1", "seed.reaction:rxn2", "seed.reaction:rxn3"]
    out = rs.similar_to_smarts("CCO>>CC=O", cands, top_k=3)  # == rxn1's reaction
    assert out[0]["reaction_id"] == "seed.reaction:rxn1"     # identical -> top
    assert out[0]["similarity"] == pytest.approx(1.0)
    assert all(d["method"] == "drfp" for d in out)


@needs_chem
def test_find_similar_smarts_routes_to_recompute(rs):
    out = rs.find_similar(
        "CCO>>CC=O",
        candidate_ids=["seed.reaction:rxn2", "seed.reaction:rxn3"],
        top_k=2,
    )
    assert out and all(d["method"] == "drfp" for d in out)


def test_find_similar_id_routes_to_berdl(rs):
    out = rs.find_similar("seed.reaction:rxn1", top_k=5)
    assert out and all(d["method"] == "berdl" for d in out)


def test_find_similar_smarts_requires_candidates(rs):
    with pytest.raises(ValueError):
        rs.find_similar("CCO>>CC=O")


# ── live integration (requires a real BERDL token) ───────────────────────

@pytest.mark.kbase
def test_live_find_similar_id_and_smarts():
    # Canonical KBase auth: KB_AUTH_TOKEN from the environment.
    from kbutillib import KBUtilLib

    rs = KBUtilLib().rxnsim
    # ID entry -> stored BERDL similarity
    out = rs.find_similar("seed.reaction:rxn00001", min_similarity=0.9, top_k=5)
    assert out and all(
        d["method"] == "berdl" and 0.9 <= d["similarity"] <= 1.0001 for d in out
    )
    # SMARTS entry -> recompute over a candidate set
    smarts = rs.resolve_to_smarts("seed.reaction:rxn00001")
    sm = rs.find_similar(
        smarts, candidate_ids=[d["reaction_id"] for d in out], top_k=3
    )
    assert all(d["method"] == "drfp" for d in sm)
