"""Unit tests for MSReconstructionUtils reaction-injection branching.

These exercise the ``template_reactions_only`` flag on
``_add_reactions_from_gene_mapping`` without standing up the full modeling
stack: the method is called unbound against a lightweight fake ``self`` whose
``biochem_db`` property returns a fake ModelSEED database, so we can assert exactly
when the database fallback is (and is not) consulted.
"""
from __future__ import annotations

import pytest

from kbutillib.domains.modeling.ms_reconstruction_utils import MSReconstructionUtils


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

    # A property, as on the real class (ms_biochem_utils.MSBiochemUtils):
    # production reads ``self.biochem_db.reactions``, never calls it.
    @property
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


# --- GPR-string support ----------------------------------------------------
#
# `_add_reactions_from_gene_mapping` accepts a dict whose values are EITHER a list
# of gene ids (historical isozyme shorthand) OR a verbatim GPR string, so that an
# upstream caller which has already computed enzyme complexes -- the KBDL model
# reconstruction service -- can hand one down without it collapsing into isozymes.
#
# Two harnesses are used below, deliberately:
#   * the `_call` fakes above store `gene_reaction_rule` verbatim, which is what
#     lets us assert BYTE-identical strings against the base commit;
#   * `_cobra_call` drives a real `cobra.Model`, which is what lets us assert the
#     normalised rule cobra actually stores, the gene objects it mints, and
#     knockout semantics.


class _FakeModelReactionIndex:
    """A model-reaction collection pre-seeded with reactions that carry GPRs."""

    def __init__(self, rule_by_id):
        self._by_id = {}
        for rid, rule in rule_by_id.items():
            rxn = _FakeReaction(rid)
            rxn.gene_reaction_rule = rule
            self._by_id[rid] = rxn

    def __contains__(self, rid):
        return rid in self._by_id

    def get_by_id(self, rid):
        return self._by_id[rid]


def _call_with_existing(existing_rules, hash_):
    """Run the method against a model that ALREADY holds the named reactions.

    Drives the existing-reaction merge branch. ``existing_rules`` maps model
    reaction ids (``"rxnA_c0"``) to their current ``gene_reaction_rule``.
    """
    template = _FakeTemplate([])
    builder = _FakeBuilder(template)
    mdlutl = _FakeMdlUtl()
    mdlutl.model.reactions = _FakeModelReactionIndex(existing_rules)
    fake_self = _FakeSelf(_CountingDB([]))
    MSReconstructionUtils._add_reactions_from_gene_mapping(
        fake_self, mdlutl, builder, hash_, template,
        template_reactions_only=True,
    )
    return mdlutl.model.reactions


def test_list_value_gpr_is_byte_identical_or_join():
    """A list value still produces exactly ``" or ".join(gene_ids)``."""
    added, _, _ = _call(
        template_ids=["rxnA_c"], hash_={"rxnA": ["g1", "g2", "g3"]},
        db_ids=[], template_reactions_only=True,
    )
    assert added[0].gene_reaction_rule == "g1 or g2 or g3"


def test_str_value_is_used_verbatim_on_a_new_reaction():
    """A str value is written straight through -- the complex is not flattened."""
    added, _, _ = _call(
        template_ids=["rxnA_c"], hash_={"rxnA": "(g1 and g2) or g3"},
        db_ids=[], template_reactions_only=True,
    )
    assert added[0].gene_reaction_rule == "(g1 and g2) or g3"


def test_mixed_list_and_str_values_in_one_hash():
    """List and str values coexist; neither is routed through the other's path."""
    added, _, _ = _call(
        template_ids=["rxnA_c", "rxnB_c"],
        hash_={"rxnA": ["g1", "g2"], "rxnB": "(g3 and g4) or g5"},
        db_ids=[], template_reactions_only=True,
    )
    by_id = {r.id: r.gene_reaction_rule for r in added}
    assert by_id["rxnA_c0"] == "g1 or g2"
    assert by_id["rxnB_c0"] == "(g3 and g4) or g5"


# --- existing-reaction merge: list input is untouched ----------------------


def test_existing_plus_incoming_list_is_byte_identical_to_base():
    """Base-commit pin: existing "a" + ["a","b"] -> exactly "a or b"."""
    rxns = _call_with_existing({"rxnA_c0": "a"}, {"rxnA": ["a", "b"]})
    assert rxns.get_by_id("rxnA_c0").gene_reaction_rule == "a or b"


def test_existing_and_rule_plus_incoming_list_is_byte_identical_to_base():
    """An existing AND-containing rule + a list merges exactly as on the base commit.

    The list branch is a flat string append and is deliberately *not* made
    and-aware: the result is the base commit's unparenthesised
    ``"<existing> or <gene>"``, which for an AND-containing existing rule is the
    historical output and is what downstream consumers already hold.
    """
    rxns = _call_with_existing({"rxnA_c0": "(x and y)"}, {"rxnA": ["z"]})
    assert rxns.get_by_id("rxnA_c0").gene_reaction_rule == "(x and y) or z"


def test_existing_empty_plus_incoming_list_is_byte_identical_to_base():
    """Empty existing rule + a list -> the plain OR join, no leading " or "."""
    rxns = _call_with_existing({"rxnA_c0": ""}, {"rxnA": ["a", "b"]})
    assert rxns.get_by_id("rxnA_c0").gene_reaction_rule == "a or b"


def test_incoming_list_gene_that_is_a_substring_is_still_skipped():
    """DEFECT PIN, not an endorsement.

    The list merge's duplicate test is ``if gene_id not in existing_gpr`` -- a
    SUBSTRING test over the rule text, not a token test. So an incoming gene id
    that happens to be a substring of a gene id already in the rule is dropped on
    the floor. Here "ab" is a substring of the existing "abc" and is lost, while
    "d" is appended.

    This is wrong, and it is preserved on purpose: changing it would silently alter
    every existing caller's GPRs. It is recorded as a defect for a separate
    migration in agent-io/work-records/.
    """
    rxns = _call_with_existing({"rxnA_c0": "abc"}, {"rxnA": ["ab", "d"]})
    assert rxns.get_by_id("rxnA_c0").gene_reaction_rule == "abc or d"


# --- existing-reaction merge: str input ------------------------------------


def test_existing_plus_incoming_str_parenthesises_both_sides():
    """Both rules are wrapped, so neither side's internal "and" re-associates."""
    GPR = pytest.importorskip("cobra.core.gene").GPR

    rxns = _call_with_existing({"rxnA_c0": "g1 or g2"}, {"rxnA": "(g3 and g4)"})
    merged = rxns.get_by_id("rxnA_c0").gene_reaction_rule
    gpr = GPR.from_string(merged)
    assert set(gpr.genes) == {"g1", "g2", "g3", "g4"}
    # Truth table of "(g1 or g2) or (g3 and g4)"; the arg is the KNOCKED-OUT set.
    assert gpr.eval(set()) is True
    assert gpr.eval({"g1"}) is True                      # g2 still carries it
    assert gpr.eval({"g1", "g2"}) is True                # complex g3+g4 carries it
    assert gpr.eval({"g1", "g2", "g3"}) is False         # complex broken, no isozyme
    assert gpr.eval({"g1", "g2", "g4"}) is False
    assert gpr.eval({"g3", "g4"}) is True                # isozymes still carry it
    # Equivalent to the intended merge, as a GPR not as text.
    reference = GPR.from_string("(g1 or g2) or (g3 and g4)")
    assert gpr.to_string() == reference.to_string()


def test_empty_existing_plus_incoming_str_takes_the_string():
    rxns = _call_with_existing({"rxnA_c0": ""}, {"rxnA": "(g1 and g2) or g3"})
    assert rxns.get_by_id("rxnA_c0").gene_reaction_rule == "(g1 and g2) or g3"


def test_existing_plus_empty_incoming_str_leaves_the_rule_unchanged():
    rxns = _call_with_existing({"rxnA_c0": "(g1 and g2) or g3"}, {"rxnA": ""})
    assert rxns.get_by_id("rxnA_c0").gene_reaction_rule == "(g1 and g2) or g3"


def test_empty_existing_plus_empty_incoming_str_stays_empty():
    rxns = _call_with_existing({"rxnA_c0": ""}, {"rxnA": ""})
    assert rxns.get_by_id("rxnA_c0").gene_reaction_rule == ""


# --- real-cobra harness ----------------------------------------------------
#
# Everything above asserts the exact text the method writes. These assert what
# cobra does with that text: the rule it normalises and stores, the Gene objects
# it mints, and whether the reaction survives a knockout. Only the template and
# builder are faked; the model is a genuine ``cobra.Model`` and the production
# method is called unmodified.


class _CobraTemplateReaction:
    """Template reaction whose ``to_reaction`` mints a real ``cobra.Reaction``."""

    def __init__(self, rxn_id):
        self.id = rxn_id
        self.metabolites = []  # empty -> the metabolite-transfer loop is a no-op

    def to_reaction(self, base_model, index):
        import cobra

        return cobra.Reaction(self.id.replace("_c", "_c0"))


class _CobraTemplate:
    def __init__(self, reaction_ids):
        self.reactions = _FakeReactionIndex([])
        self.reactions._by_id = {
            rid: _CobraTemplateReaction(rid) for rid in reaction_ids
        }
        self.compartments = {}


class _CobraBuilder:
    def __init__(self, template, model):
        self.template = template
        self.base_model = model
        self.index = "0"
        self.compartments = {}
        self.template_species_to_model_species = {}


class _CobraMdlUtl:
    def __init__(self, model):
        self.model = model


def _cobra_call(template_ids, hash_, existing_rules=None):
    """Drive the method against a real ``cobra.Model``; return that model."""
    cobra = pytest.importorskip("cobra")

    model = cobra.Model("test_model")
    for rid, rule in (existing_rules or {}).items():
        rxn = cobra.Reaction(rid)
        model.add_reactions([rxn])
        model.reactions.get_by_id(rid).gene_reaction_rule = rule
    template = _CobraTemplate(template_ids)
    builder = _CobraBuilder(template, model)
    mdlutl = _CobraMdlUtl(model)
    fake_self = _FakeSelf(_CountingDB([]))
    MSReconstructionUtils._add_reactions_from_gene_mapping(
        fake_self, mdlutl, builder, hash_, template,
        template_reactions_only=True,
    )
    return model


def test_str_gpr_is_stored_as_cobra_normalises_it():
    """The stored rule is cobra's normalised form of the verbatim string."""
    GPR = pytest.importorskip("cobra.core.gene").GPR

    model = _cobra_call(["rxnA_c"], {"rxnA": "(g1 and g2) or g3"})
    rxn = model.reactions.get_by_id("rxnA_c0")
    assert rxn.gene_reaction_rule == GPR.from_string("(g1 and g2) or g3").to_string()
    assert rxn.gene_reaction_rule == "(g1 and g2) or g3"


def test_str_gpr_registers_every_gene_on_the_model():
    """All three genes of the complex reach ``model.genes`` -- none is lost."""
    model = _cobra_call(["rxnA_c"], {"rxnA": "(g1 and g2) or g3"})
    assert {g.id for g in model.genes} >= {"g1", "g2", "g3"}
    assert {g.id for g in model.reactions.get_by_id("rxnA_c0").genes} == {
        "g1", "g2", "g3",
    }


def test_str_gpr_has_complex_knockout_semantics():
    """The complex survives: KO of g1 alone does not block the reaction, g1+g3 does.

    Asserted twice over -- once through ``GPR.eval`` with an explicit knockout set,
    once through ``reaction.functional`` after flipping ``Gene.functional`` -- so
    this holds for both of cobra's knockout entry points.
    """
    model = _cobra_call(["rxnA_c"], {"rxnA": "(g1 and g2) or g3"})
    rxn = model.reactions.get_by_id("rxnA_c0")

    # GPR.eval takes the KNOCKED-OUT gene set.
    assert rxn.gpr.eval(set()) is True
    assert rxn.gpr.eval({"g1"}) is True            # g3 isozyme still carries it
    assert rxn.gpr.eval({"g3"}) is True            # g1+g2 complex still carries it
    assert rxn.gpr.eval({"g1", "g3"}) is False     # both routes gone
    assert rxn.gpr.eval({"g2", "g3"}) is False

    # Same thing via Gene.functional / Reaction.functional.
    model.genes.get_by_id("g1").functional = False
    assert rxn.functional is True
    model.genes.get_by_id("g3").functional = False
    assert rxn.functional is False


def test_list_value_still_flattens_to_isozymes_under_cobra():
    """A list value keeps pure-OR (isozyme) semantics: any one gene suffices."""
    model = _cobra_call(["rxnA_c"], {"rxnA": ["g1", "g2", "g3"]})
    rxn = model.reactions.get_by_id("rxnA_c0")
    assert rxn.gene_reaction_rule == "g1 or g2 or g3"
    assert rxn.gpr.eval({"g1", "g2"}) is True      # g3 alone is enough
    assert rxn.gpr.eval({"g1", "g2", "g3"}) is False


def test_str_merge_onto_an_existing_cobra_reaction():
    """Existing "g1 or g2" + incoming "(g3 and g4)" under a real cobra model."""
    model = _cobra_call(
        [], {"rxnA": "(g3 and g4)"}, existing_rules={"rxnA_c0": "g1 or g2"}
    )
    rxn = model.reactions.get_by_id("rxnA_c0")
    assert {g.id for g in rxn.genes} == {"g1", "g2", "g3", "g4"}
    assert rxn.gpr.eval({"g1", "g2"}) is True            # complex carries it
    assert rxn.gpr.eval({"g3"}) is True                  # isozymes carry it
    assert rxn.gpr.eval({"g1", "g2", "g3"}) is False     # both routes gone


def test_model_gene_count_consumer_is_correct_for_a_str_value():
    """AUDITED CONSUMER: ``len(mdlutl.model.genes)`` (ms_reconstruction_utils.py,
    ``build_metabolic_model`` -> ``current_output["Model genes"]``).

    It reads cobra's parsed gene set rather than splitting rule text, so it is
    already correct for a verbatim GPR. Pinned here because the naive
    ``rule.split(" or ")`` it could have been would report 2 for this complex
    instead of 3.
    """
    GPR = pytest.importorskip("cobra.core.gene").GPR

    model = _cobra_call(["rxnA_c"], {"rxnA": "(g1 and g2) or g3"})
    assert len(model.genes) == 3
    rule = model.reactions.get_by_id("rxnA_c0").gene_reaction_rule
    assert set(GPR.from_string(rule).genes) == {"g1", "g2", "g3"}
    # The text-splitting alternative is wrong, which is why it is not used.
    assert len(rule.split(" or ")) == 2


def test_reactions_to_add_gate_is_truthy_for_str_values():
    """AUDITED CONSUMER: ``if reactions_to_add:`` in ``build_metabolic_model``.

    A plain truthiness gate, so a dict of str values passes it exactly as a dict of
    list values does -- but an EMPTY str value must not make the whole dict falsy,
    and must not be mistaken for "no reactions".
    """
    assert bool({"rxnA": "(g1 and g2) or g3"}) is True
    assert bool({"rxnA": ""}) is True
    assert bool({}) is False


def test_gpr_string_support_marker_is_exported():
    """The capability marker KBDL reads to gate complex-bearing jobs."""
    from kbutillib.domains.modeling.ms_reconstruction_utils import GPR_STRING_SUPPORT

    assert GPR_STRING_SUPPORT == 1
    assert isinstance(GPR_STRING_SUPPORT, int)
    assert not isinstance(GPR_STRING_SUPPORT, bool)


def test_gpr_string_support_marker_is_reexported_by_the_flat_shim():
    """The deprecated flat module must expose the same object, not a copy."""
    import warnings

    from kbutillib.domains.modeling.ms_reconstruction_utils import GPR_STRING_SUPPORT

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        from kbutillib.ms_reconstruction_utils import (
            GPR_STRING_SUPPORT as shim_marker,
        )

    assert shim_marker == GPR_STRING_SUPPORT == 1
