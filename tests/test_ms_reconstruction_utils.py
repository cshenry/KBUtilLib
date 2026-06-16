"""Unit tests for MSReconstructionUtils reaction-injection branching.

These exercise the ``template_reactions_only`` flag on
``_add_reactions_from_gene_mapping`` without standing up the full modeling
stack: the method is called unbound against a lightweight fake ``self`` whose
``biochem_db()`` returns a fake ModelSEED database, so we can assert exactly
when the database fallback is (and is not) consulted.
"""
from __future__ import annotations

from kbutillib.ms_reconstruction_utils import MSReconstructionUtils


# --- fakes -----------------------------------------------------------------


class _FakeReaction:
    def __init__(self, rxn_id):
        self.id = rxn_id
        self.gene_reaction_rule = ""
        self.annotation = {}
        self.metabolites = {}


class _FakeTemplateReaction:
    """Stands in for a template reaction; ``to_reaction`` mints a model reaction."""

    def __init__(self, rxn_id):
        self.id = rxn_id
        self.metabolites = []  # empty -> the metabolite-transfer loop is a no-op

    def to_reaction(self, base_model, index):
        return _FakeReaction(self.id.replace("_c", "_c0"))


class _FakeReactionIndex:
    """Minimal ``x in container`` / ``container.get_by_id(x)`` collection."""

    def __init__(self, ids):
        self._by_id = {rid: _FakeTemplateReaction(rid) for rid in ids}

    def __contains__(self, rid):
        return rid in self._by_id

    def get_by_id(self, rid):
        return self._by_id[rid]


class _FakeModel:
    def __init__(self):
        self.reactions = _FakeReactionIndex([])  # nothing present yet
        self.added = []

    def add_reactions(self, rxns):
        self.added.extend(rxns)


class _FakeMdlUtl:
    def __init__(self):
        self.model = _FakeModel()


class _FakeTemplate:
    def __init__(self, reaction_ids, compartments=None):
        self.reactions = _FakeReactionIndex(reaction_ids)
        self.compartments = compartments or {}


class _FakeBuilder:
    def __init__(self, template):
        self.template = template
        self.base_model = _FakeModel()
        self.index = "0"
        self.compartments = {}
        self.template_species_to_model_species = {}


class _FakeDBReaction:
    """A mass-balanced (status free of 'MI'/'CI') ModelSEED DB reaction."""

    def __init__(self, rxn_id):
        self.id = rxn_id
        self.status = "OK"

    def to_template_reaction(self, compartment_map):
        return _FakeTemplateReaction(self.id + "_c")


class _CountingDB:
    """Fake ModelSEED DB that records whether its reactions were consulted."""

    def __init__(self, reaction_ids):
        self.consulted = False
        self._ids = set(reaction_ids)
        self.reactions = self

    def __contains__(self, rid):  # `rid in modelseeddb.reactions`
        self.consulted = True
        return rid in self._ids

    def get_by_id(self, rid):
        return _FakeDBReaction(rid)


class _FakeSelf:
    """Lightweight stand-in for the MSReconstructionUtils instance."""

    def __init__(self, db):
        self._db = db

    def biochem_db(self):
        return self._db


def _call(template_ids, hash_, db_ids, *, template_reactions_only):
    template = _FakeTemplate(template_ids)
    builder = _FakeBuilder(template)
    mdlutl = _FakeMdlUtl()
    db = _CountingDB(db_ids)
    fake_self = _FakeSelf(db)
    added = MSReconstructionUtils._add_reactions_from_gene_mapping(
        fake_self, mdlutl, builder, hash_, template,
        template_reactions_only=template_reactions_only,
    )
    return added, db, mdlutl


def test_template_reactions_only_skips_db_fallback():
    # rxnA is in the template; rxnB is only in the ModelSEED DB.
    hash_ = {"rxnA": ["g1"], "rxnB": ["g2"]}
    added, db, _ = _call(
        template_ids=["rxnA_c"], hash_=hash_, db_ids=["rxnB"],
        template_reactions_only=True,
    )
    # Only the template reaction is added; rxnB is dropped silently.
    assert [r.id for r in added] == ["rxnA_c0"]
    # The DB fallback was never consulted.
    assert db.consulted is False
    # The added reaction carries the OR'd GPR.
    assert added[0].gene_reaction_rule == "g1"


def test_default_mode_uses_db_fallback():
    # Same inputs, default mode: rxnB falls back to the (mass-balanced) DB.
    hash_ = {"rxnA": ["g1"], "rxnB": ["g2"]}
    added, db, _ = _call(
        template_ids=["rxnA_c"], hash_=hash_, db_ids=["rxnB"],
        template_reactions_only=False,
    )
    assert {r.id for r in added} == {"rxnA_c0", "rxnB_c0"}
    assert db.consulted is True
