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


# ---------------------------------------------------------------------------
# Exchange reactions must exist BEFORE ATP correction runs
# (KBDLJobRunningPrototype issue #28, cause 1)
#
# ``MSBuilder.build()`` calls ``add_exchanges_to_model`` from inside itself,
# before ``build_metabolic_model`` has injected ``reactions_to_add``.  On the
# RAST-role path that is harmless, because ``build_metabolic_reactions`` runs
# earlier inside ``build()``.  On a ROLELESS build -- the path GAA's
# ``build_isolated_model`` and the KBDL reconstruction job use -- ``build()``
# contributes no metabolic reactions at all, so the model reached ATP
# correction with reactions but ZERO exchanges.  With no exchanges there is no
# nutrient uptake: ATP was 0 on every one of the 54 ATP media, all 54 were
# recorded ``{"score": 1000, "failed": true}``, ``selected_media`` came out
# empty, nothing raised, and the model shipped with energy-generating cycles.
#
# These tests pin the ORDER, which is the whole of the fix, using a call
# recorder rather than a real builder/corrector.
# ---------------------------------------------------------------------------


class _OrderRecorder:
    """Shared call log for the fakes below."""

    def __init__(self):
        self.calls: list[str] = []
        # Every ``allow_all_non_grp_reactions`` value the builder was asked for,
        # in call order (issue #29 -- the flag must stay False).
        self.non_grp_flags: list[bool] = []


class _OrderedModel:
    def __init__(self, recorder):
        self._recorder = recorder
        self.reactions = []
        self.genes = []
        self.objective = None

    def remove_reactions(self, ids):  # pragma: no cover - not exercised here
        pass

    def add_reactions(self, reactions):
        self._recorder.calls.append("model.add_reactions")
        self.reactions.extend(reactions)


class _OrderedMdlUtl:
    def __init__(self, recorder):
        self.model = _OrderedModel(recorder)
        self._recorder = recorder
        self._attributes: dict = {}
        self.wsid = None
        self.recon_builder = None
        self.atp_tests = None

    def get_attributes(self):
        return self._attributes

    def nonexchange_reaction_count(self):
        return 0

    def is_core(self, rxn):  # pragma: no cover - not exercised here
        return True


class _OrderedBuilder:
    """Stands in for ``MSBuilder``: both the class and its instances."""

    def __init__(self, recorder):
        self._recorder = recorder

    # class-level use: ``self.MSBuilder(genome, template)``
    def __call__(self, genome, template):
        self._recorder.calls.append("build_instance")
        return self

    # instance-level use: ``builder.build(base_model, "0", False, False)``
    def build(self, *args, **kwargs):
        self._recorder.calls.append("builder.build")
        return _OrderedModel(self._recorder)

    # instance-level use: ``builder.build_non_metabolite_reactions(model, ...)``
    def build_non_metabolite_reactions(
        self, cobra_model, allow_all_non_grp_reactions=False
    ):
        self._recorder.calls.append("build_non_metabolite_reactions")
        self._recorder.non_grp_flags.append(allow_all_non_grp_reactions)
        return []

    # class-level static use: ``self.MSBuilder.add_exchanges_to_model(model)``
    def add_exchanges_to_model(self, model, extra_cell="e0"):
        self._recorder.calls.append("add_exchanges_to_model")
        return []


class _OrderedATPCorrection:
    def __init__(self, recorder):
        self._recorder = recorder
        self.cumulative_core_gapfilling = []
        self.sentinel_tests = [{"media": None, "objective": "rxn00062_c0"}]

    def __call__(self, *args, **kwargs):
        self._recorder.calls.append("MSATPCorrection.__init__")
        return self

    def run_atp_correction(self):
        self._recorder.calls.append("run_atp_correction")
        return self.sentinel_tests


class _OrderedSelf:
    """Fake MSReconstructionUtils carrying only what the build path touches."""

    def __init__(self, recorder, atp):
        self.MSBuilder = _OrderedBuilder(recorder)
        self.MSATPCorrection = atp
        self.MSModelUtil = self
        self.modelseedpy_data_dir = "/nonexistent"
        self.templates = {"core": "core_ref", "gn": "gn_ref"}
        self._recorder = recorder
        self._mdlutl = _OrderedMdlUtl(recorder)

    # ``self.MSModelUtil.get(mdl)``
    def get(self, mdl):
        return self._mdlutl

    def get_template(self, ref, _none):
        return object()

    def _add_reactions_from_gene_mapping(self, mdlutl, builder, hash_,
                                         template, **kwargs):
        self._recorder.calls.append("_add_reactions_from_gene_mapping")
        return []


class _OrderedGenome:
    id = "g1"
    scientific_name = "Toy organism"
    features: list = []


def _build_with_recorder(*, atp_safe=True, reactions_to_add=None):
    recorder = _OrderRecorder()
    atp = _OrderedATPCorrection(recorder)
    fake_self = _OrderedSelf(recorder, atp)
    import cobra  # noqa: F401 - build_metabolic_model imports cobra lazily

    out, mdlutl = MSReconstructionUtils.build_metabolic_model(
        fake_self,
        genome=_OrderedGenome(),
        genome_classifier=None,
        gs_template="gn",
        gs_template_obj=object(),
        core_template=object(),
        atp_safe=atp_safe,
        reactions_to_add=reactions_to_add
        if reactions_to_add is not None
        else {"rxn00001": ["g1"]},
    )
    return recorder.calls, out, mdlutl, atp


def test_exchanges_are_added_before_atp_correction_runs():
    """The ordering that issue #28 cause 1 is entirely about."""
    pytest.importorskip("cobra")
    calls, _out, _mdlutl, _atp = _build_with_recorder()

    assert "add_exchanges_to_model" in calls
    assert "run_atp_correction" in calls
    assert calls.index("add_exchanges_to_model") < calls.index(
        "run_atp_correction"
    ), f"exchanges must precede ATP correction; order was {calls}"


def test_exchanges_are_added_after_the_injected_reactions():
    """Order matters on BOTH sides.

    The injected reactions are what introduce the extracellular metabolites,
    so adding exchanges before them would build none of the exchanges that
    matter -- which is exactly what ``MSBuilder.build()``'s own internal call
    already does.
    """
    pytest.importorskip("cobra")
    calls, _out, _mdlutl, _atp = _build_with_recorder()

    assert calls.index("_add_reactions_from_gene_mapping") < calls.index(
        "add_exchanges_to_model"
    ), f"injection must precede exchange creation; order was {calls}"


def test_exchanges_are_added_even_when_atp_correction_is_skipped():
    """``atp_safe=False`` must still produce a model that can take up
    nutrients, or the gap-fill that follows has nothing to work with."""
    pytest.importorskip("cobra")
    calls, _out, _mdlutl, _atp = _build_with_recorder(atp_safe=False)

    assert "add_exchanges_to_model" in calls
    assert "run_atp_correction" not in calls


def test_exchanges_are_added_when_there_are_no_reactions_to_add():
    """The call is unconditional, not gated on ``reactions_to_add``."""
    pytest.importorskip("cobra")
    calls, _out, _mdlutl, _atp = _build_with_recorder(reactions_to_add={})

    assert "_add_reactions_from_gene_mapping" not in calls
    assert "add_exchanges_to_model" in calls
    assert calls.index("add_exchanges_to_model") < calls.index(
        "run_atp_correction"
    )


def test_atp_tests_are_published_on_the_model_util():
    """The tests ``run_atp_correction`` returns were computed and dropped.

    GAA's ``gapfill_reaction_set`` needs them to make genome-scale gap-fill
    ATP-safe (issue #28 cause 3), and recomputing them costs a full ATP run.
    """
    pytest.importorskip("cobra")
    _calls, _out, mdlutl, atp = _build_with_recorder()

    assert mdlutl.atp_tests == atp.sentinel_tests


def test_no_atp_tests_are_published_when_correction_is_skipped():
    """``atp_safe=False`` leaves the attribute untouched, so a downstream
    consumer can tell "not computed" from "computed and empty"."""
    pytest.importorskip("cobra")
    _calls, _out, mdlutl, _atp = _build_with_recorder(atp_safe=False)

    assert mdlutl.atp_tests is None


# ---------------------------------------------------------------------------
# Spontaneous/universal template reactions must be revisited AFTER gene mapping
# (KBDLJobRunningPrototype issue #29)
#
# ``MSBuilder.build_non_metabolite_reactions`` admits a spontaneous/universal
# template reaction only when at least one of its metabolites is already a key
# of ``builder.template_species_to_model_species`` (unless
# ``allow_all_non_grp_reactions=True``).  ``build()`` calls it once, from inside
# itself.  On the ROLELESS gene-mapping path ``build()`` adds no metabolic
# reactions at all, so that map is still empty when the predicate runs and
# almost nothing qualifies: models were getting 3-12 of the 42
# spontaneous/universal reactions in GramNeg V6.  The casualties include
# membrane diffusion of O2 (``rxn05468``), CO2 (``rxn05467``) and water
# (``rxn05319``), so the models could not respire -- they "grew" by running
# cytochrome oxidase ``rxn14415`` backwards to make O2 out of water.  The real
# reactions arrive afterwards from ``_add_reactions_from_gene_mapping``, and
# before this fix nothing revisited the spontaneous set after that.
#
# The ordering tests below reuse the ``_Ordered*`` recorder.  The behavioural
# tests use ``_SpontaneousBuilder``, which reimplements the modelseedpy 0.4.2
# predicate faithfully over real cobra objects, so they assert on the FINISHED
# MODEL rather than on a call log.
# ---------------------------------------------------------------------------


def test_non_metabolite_reactions_are_rebuilt_after_gene_mapping():
    """The ``AFTER`` half of the fix's position.

    Gene mapping is what populates ``template_species_to_model_species``, so a
    call that precedes it sees an empty map and admits nothing.
    """
    pytest.importorskip("cobra")
    calls, _out, _mdlutl, _atp = _build_with_recorder()

    assert "build_non_metabolite_reactions" in calls
    assert calls.index("_add_reactions_from_gene_mapping") < calls.index(
        "build_non_metabolite_reactions"
    ), f"gene mapping must precede the rebuild; order was {calls}"


def test_non_metabolite_reactions_are_rebuilt_before_exchanges():
    """The ``BEFORE`` half of the fix's position.

    An extracellular species introduced by a newly added diffusion reaction has
    to reach the single existing ``add_exchanges_to_model`` call, or it becomes
    a flux dead-end.
    """
    pytest.importorskip("cobra")
    calls, _out, _mdlutl, _atp = _build_with_recorder()

    assert calls.index("build_non_metabolite_reactions") < calls.index(
        "add_exchanges_to_model"
    ), f"rebuild must precede exchange creation; order was {calls}"


def test_non_metabolite_reactions_are_rebuilt_before_atp_correction():
    """Respiration has to exist before ATP correction judges the media."""
    pytest.importorskip("cobra")
    calls, _out, _mdlutl, _atp = _build_with_recorder()

    assert calls.index("build_non_metabolite_reactions") < calls.index(
        "run_atp_correction"
    ), f"rebuild must precede ATP correction; order was {calls}"


def _build_capturing_recorder(*, atp_safe=True, reactions_to_add=None):
    """As ``_build_with_recorder``, but hands back the recorder itself.

    Needed because the ``allow_all_non_grp_reactions`` assertion is about an
    ARGUMENT, not about call order, and the older helper only exposes the log.
    """
    recorder = _OrderRecorder()
    fake_self = _OrderedSelf(recorder, _OrderedATPCorrection(recorder))
    import cobra  # noqa: F401 - build_metabolic_model imports cobra lazily

    MSReconstructionUtils.build_metabolic_model(
        fake_self,
        genome=_OrderedGenome(),
        genome_classifier=None,
        gs_template="gn",
        gs_template_obj=object(),
        core_template=object(),
        atp_safe=atp_safe,
        reactions_to_add=reactions_to_add
        if reactions_to_add is not None
        else {"rxn00001": ["g1"]},
    )
    return recorder


def test_rebuild_never_forces_all_non_grp_reactions():
    """``allow_all_non_grp_reactions`` stays False, deliberately.

    Forcing all 42 spontaneous/universal reactions in would guarantee the count
    but would add reactions whose metabolites are absent, creating dead-end
    species and destroying the coverage number as a diagnostic signal.  The
    conservative predicate is the point of the fix, not a limitation of it.
    """
    pytest.importorskip("cobra")
    recorder = _build_capturing_recorder()

    assert recorder.non_grp_flags, "the builder was never asked to rebuild"
    assert all(flag is False for flag in recorder.non_grp_flags), (
        "allow_all_non_grp_reactions must stay False; "
        f"saw {recorder.non_grp_flags}"
    )


# --- behavioural half: assert on the FINISHED MODEL ------------------------
#
# The fakes below reimplement the modelseedpy 0.4.2 inclusion predicate rather
# than recording calls, so these tests fail if the second call is present but
# ineffective -- which it would be if its return value were dropped.
# ``build_non_metabolite_reactions`` RETURNS reactions and does not add them;
# ``build()`` pairs it with its own ``cobra_model.add_reactions`` and the fix
# has to do the same.


#: Membrane diffusion reactions from GramNeg V6, as
#: ``template reaction id -> {template species id: coefficient}``.  These three
#: are the ones issue #29 measured: their absence is why models could not
#: respire.
_DIFFUSION_TEMPLATE_REACTIONS = {
    "rxn05468_c": {"cpd00007_c": -1.0, "cpd00007_e": 1.0},  # O2
    "rxn05467_c": {"cpd00011_c": -1.0, "cpd00011_e": 1.0},  # CO2
    "rxn05319_c": {"cpd00001_c": -1.0, "cpd00001_e": 1.0},  # H2O
}


def _mint_model_metabolite(template_species_id):
    """``cpd00007_c`` -> a cobra metabolite ``cpd00007_c0`` in compartment ``c0``.

    Mirrors ``MSTemplateSpecies.to_metabolite(index)``: the template species id
    carries a bare compartment letter and the model metabolite id carries the
    compartment plus the builder index.
    """
    import cobra

    base, _, compartment = template_species_id.rpartition("_")
    model_compartment = compartment + "0"
    return cobra.Metabolite(
        id=f"{base}_{model_compartment}",
        name=base,
        compartment=model_compartment,
    )


class _SpontaneousBuilder:
    """Predicate-faithful stand-in for ``MSBuilder`` on the roleless path.

    Reproduces three behaviours of modelseedpy 0.4.2 that the fix depends on:

    * ``build()`` on a roleless genome adds NO metabolic reactions, so the
      ``template_species_to_model_species`` map is still empty when ``build()``
      makes its own internal ``build_non_metabolite_reactions`` call -- and the
      predicate therefore admits nothing.
    * ``build_non_metabolite_reactions`` admits a reaction only when at least
      one of its template metabolites is already a key of that map (or
      ``allow_all_non_grp_reactions`` is set), and it RETURNS the reactions
      instead of adding them.
    * ``add_exchanges_to_model`` mints ``EX_<metabolite id>`` for every
      metabolite in the ``e0`` compartment, skipping ids already present.
    """

    def __init__(self, template_reactions=None):
        self._template_reactions = (
            _DIFFUSION_TEMPLATE_REACTIONS
            if template_reactions is None
            else template_reactions
        )
        self.template_species_to_model_species = {}
        self.base_model = None
        self.index = "0"
        self.compartments = {}
        #: number of reactions each ``build_non_metabolite_reactions`` call
        #: admitted, in call order -- lets a test see the first call return
        #: nothing and a later one return the diffusion reactions.
        self.admitted_per_call = []

    # ``self.MSBuilder(genome, template)``
    def __call__(self, genome, template):
        return self

    # ``builder.build(base_model, "0", False, False)``
    def build(self, model_or_id, index="0", *args, **kwargs):
        self.index = index
        self.base_model = model_or_id
        # The roleless path contributes no metabolic reactions at all; the only
        # thing build() does that matters here is its own (futile) rebuild.
        model_or_id.add_reactions(self.build_non_metabolite_reactions(model_or_id))
        self.add_exchanges_to_model(model_or_id)
        return model_or_id

    def build_non_metabolite_reactions(
        self, cobra_model, allow_all_non_grp_reactions=False
    ):
        import cobra

        reactions = []
        for template_rxn_id, stoichiometry in self._template_reactions.items():
            species_ids = set(stoichiometry)
            if not (
                species_ids & set(self.template_species_to_model_species)
                or allow_all_non_grp_reactions
            ):
                continue
            model_stoichiometry = {}
            for species_id, coefficient in stoichiometry.items():
                if species_id not in self.template_species_to_model_species:
                    metabolite = _mint_model_metabolite(species_id)
                    self.template_species_to_model_species[species_id] = metabolite
                    self.base_model.add_metabolites([metabolite])
                model_stoichiometry[
                    self.template_species_to_model_species[species_id]
                ] = coefficient
            model_rxn_id = template_rxn_id + "0"
            if model_rxn_id not in cobra_model.reactions:
                reaction = cobra.Reaction(
                    model_rxn_id, name=model_rxn_id, lower_bound=-1000, upper_bound=1000
                )
                reaction.add_metabolites(model_stoichiometry)
                reactions.append(reaction)
        self.admitted_per_call.append(len(reactions))
        return reactions

    # ``self.MSBuilder.add_exchanges_to_model(mdlutl.model)`` -- a staticmethod
    # on the real class, so the model arrives as the first positional argument.
    def add_exchanges_to_model(self, model, extra_cell="e0"):
        import cobra

        exchanges = []
        for metabolite in model.metabolites:
            if metabolite.compartment != extra_cell:
                continue
            exchange_id = "EX_" + metabolite.id
            if exchange_id in model.reactions:
                continue
            exchange = cobra.Reaction(
                exchange_id,
                name="Exchange for " + metabolite.name,
                lower_bound=-1000,
                upper_bound=1000,
            )
            exchange.add_metabolites({metabolite: -1})
            exchanges.append(exchange)
        model.add_reactions(exchanges)
        return exchanges


class _SpontaneousMdlUtl:
    def __init__(self, model):
        self.model = model
        self._attributes: dict = {}
        self.wsid = None
        self.recon_builder = None
        self.atp_tests = None

    def get_attributes(self):
        return self._attributes

    def nonexchange_reaction_count(self):
        return len(self.model.reactions)

    def is_core(self, rxn):  # pragma: no cover - not exercised here
        return True


class _SpontaneousSelf:
    """Fake ``MSReconstructionUtils`` whose gene mapping seeds chosen species.

    ``mapped_species`` is the set of TEMPLATE species ids the gene-mapped
    reactions bring into the model -- i.e. exactly what the real
    ``_add_reactions_from_gene_mapping`` writes into
    ``builder.template_species_to_model_species`` as it goes.
    """

    def __init__(self, builder, mapped_species):
        self.MSBuilder = builder
        self.MSATPCorrection = None
        self.MSModelUtil = self
        self.modelseedpy_data_dir = "/nonexistent"
        self.templates = {"core": "core_ref", "gn": "gn_ref"}
        self._mapped_species = mapped_species
        self._mdlutl = None

    def get(self, mdl):
        self._mdlutl = _SpontaneousMdlUtl(mdl)
        return self._mdlutl

    def get_template(self, ref, _none):
        return object()

    def _add_reactions_from_gene_mapping(self, mdlutl, builder, hash_, template,
                                        **kwargs):
        """Faithful to the real method's map-writing half.

        The real method mints a model metabolite for every template metabolite
        it has not seen, registers it in
        ``builder.template_species_to_model_species`` and adds it to
        ``builder.base_model`` -- which is what makes the predicate in the
        second ``build_non_metabolite_reactions`` call able to admit anything.
        """
        import cobra

        model_stoichiometry = {}
        for species_id in self._mapped_species:
            if species_id not in builder.template_species_to_model_species:
                metabolite = _mint_model_metabolite(species_id)
                builder.template_species_to_model_species[species_id] = metabolite
                builder.base_model.add_metabolites([metabolite])
            model_stoichiometry[
                builder.template_species_to_model_species[species_id]
            ] = -1.0
        if not model_stoichiometry:
            return []
        reaction = cobra.Reaction(
            "rxn00001_c0", name="gene-mapped reaction",
            lower_bound=-1000, upper_bound=1000,
        )
        reaction.add_metabolites(model_stoichiometry)
        reaction.gene_reaction_rule = "g1"
        mdlutl.model.add_reactions([reaction])
        return [reaction]


def _build_with_spontaneous(mapped_species):
    """Run a roleless gene-mapped build and return ``(model, builder)``."""
    import cobra

    builder = _SpontaneousBuilder()
    fake_self = _SpontaneousSelf(builder, mapped_species)
    base_model = cobra.Model(id_or_model="g1", name="Toy organism")

    _out, mdlutl = MSReconstructionUtils.build_metabolic_model(
        fake_self,
        genome=_OrderedGenome(),
        genome_classifier=None,
        gs_template="gn",
        gs_template_obj=object(),
        core_template=object(),
        base_model=base_model,
        atp_safe=False,
        reactions_to_add={"rxn00001": ["g1"]},
    )
    return mdlutl.model, builder


def test_the_first_rebuild_inside_build_admits_nothing_on_a_roleless_genome():
    """Why a second call is needed at all.

    This is the defect, stated as a property: on the roleless path the model is
    still empty when ``build()`` runs its own rebuild, so the predicate has an
    empty ``template_species_to_model_species`` to intersect against and admits
    zero of the spontaneous/universal reactions.
    """
    pytest.importorskip("cobra")
    _model, builder = _build_with_spontaneous({"cpd00007_c", "cpd00011_c"})

    assert builder.admitted_per_call[0] == 0, (
        "build()'s own rebuild should admit nothing on a roleless genome; "
        f"per-call admissions were {builder.admitted_per_call}"
    )


def test_o2_and_co2_diffusion_are_present_after_a_gene_mapped_build():
    """(a) The reactions issue #29 is named for.

    Gene mapping brings the cytosolic O2 (``cpd00007_c``) and CO2
    (``cpd00011_c``) species into the model, so the rebuild's predicate can now
    admit ``rxn05468`` (O2 membrane diffusion) and ``rxn05467`` (CO2 membrane
    diffusion).  Without the second call these are absent and the model cannot
    respire -- it "grows" by running cytochrome oxidase ``rxn14415`` backwards
    to make O2 out of water.
    """
    pytest.importorskip("cobra")
    model, _builder = _build_with_spontaneous({"cpd00007_c", "cpd00011_c"})

    reaction_ids = {rxn.id for rxn in model.reactions}
    assert "rxn05468_c0" in reaction_ids, (
        "O2 membrane diffusion missing; the model cannot respire. "
        f"reactions were {sorted(reaction_ids)}"
    )
    assert "rxn05467_c0" in reaction_ids, (
        "CO2 membrane diffusion missing. "
        f"reactions were {sorted(reaction_ids)}"
    )


def test_an_unmapped_species_still_rides_in_on_a_mapped_reaction():
    """The predicate needs only ONE metabolite in common, not all of them.

    Water was never gene-mapped here, yet ``rxn05319`` stays out because none of
    its species are in the map -- which is the conservative behaviour
    ``allow_all_non_grp_reactions=False`` buys, and what keeps the coverage
    number meaningful as a diagnostic.
    """
    pytest.importorskip("cobra")
    model, _builder = _build_with_spontaneous({"cpd00007_c"})

    reaction_ids = {rxn.id for rxn in model.reactions}
    assert "rxn05468_c0" in reaction_ids  # O2 was mapped
    assert "rxn05319_c0" not in reaction_ids  # water was not, and is not forced


def test_diffusion_introduced_extracellular_species_gets_an_exchange():
    """(b) The ordering property, asserted on the model rather than a line number.

    ``rxn05468`` introduces extracellular O2 (``cpd00007_e0``), a species that
    did not exist until the rebuild ran.  Because the rebuild is positioned
    BEFORE the single ``add_exchanges_to_model`` call, that species gets its
    exchange; if the rebuild were placed after it, O2 would be a flux dead-end
    and the respiration this fix restores would still not work.
    """
    pytest.importorskip("cobra")
    model, _builder = _build_with_spontaneous({"cpd00007_c", "cpd00011_c"})

    metabolite_ids = {m.id for m in model.metabolites}
    assert "cpd00007_e0" in metabolite_ids, "the diffusion reaction was not added"

    reaction_ids = {rxn.id for rxn in model.reactions}
    assert "EX_cpd00007_e0" in reaction_ids, (
        "extracellular O2 introduced by the rebuild has no exchange, so it is a "
        f"flux dead-end. reactions were {sorted(reaction_ids)}"
    )
    assert "EX_cpd00011_e0" in reaction_ids, (
        "extracellular CO2 introduced by the rebuild has no exchange. "
        f"reactions were {sorted(reaction_ids)}"
    )


def test_every_extracellular_species_has_an_exchange():
    """Stated as a whole-model invariant, so it cannot pass by coincidence."""
    pytest.importorskip("cobra")
    model, _builder = _build_with_spontaneous({"cpd00007_c", "cpd00011_c"})

    reaction_ids = {rxn.id for rxn in model.reactions}
    extracellular = [m.id for m in model.metabolites if m.compartment == "e0"]
    assert extracellular, "no extracellular species were introduced at all"
    missing = [m for m in extracellular if "EX_" + m not in reaction_ids]
    assert not missing, f"extracellular species with no exchange: {missing}"


def test_a_mapping_with_no_relevant_species_is_a_no_op():
    """(c) The predicate admits nothing and nothing raises.

    A genome whose gene mapping brings in none of the diffusion species must be
    left exactly as it was: the conservative predicate is what prevents the
    second call from inventing dead-end metabolites, so "admits nothing" is a
    correct outcome, not a failure.
    """
    pytest.importorskip("cobra")
    model, builder = _build_with_spontaneous({"cpd99999_c"})

    reaction_ids = {rxn.id for rxn in model.reactions}
    assert not reaction_ids & {"rxn05468_c0", "rxn05467_c0", "rxn05319_c0"}, (
        f"nothing should have been admitted; reactions were {sorted(reaction_ids)}"
    )
    # The rebuild ran and simply found nothing to do.
    assert builder.admitted_per_call == [0, 0]
    # No dead-end extracellular species were invented.
    assert not [m.id for m in model.metabolites if m.compartment == "e0"]


def test_an_empty_gene_mapping_does_not_raise():
    """``reactions_to_add={}`` skips gene mapping entirely; the rebuild still
    runs, still admits nothing, and must not raise."""
    pytest.importorskip("cobra")
    import cobra

    builder = _SpontaneousBuilder()
    fake_self = _SpontaneousSelf(builder, set())
    _out, mdlutl = MSReconstructionUtils.build_metabolic_model(
        fake_self,
        genome=_OrderedGenome(),
        genome_classifier=None,
        gs_template="gn",
        gs_template_obj=object(),
        core_template=object(),
        base_model=cobra.Model(id_or_model="g1", name="Toy organism"),
        atp_safe=False,
        reactions_to_add={},
    )

    assert len(mdlutl.model.reactions) == 0
    assert builder.admitted_per_call == [0, 0]


def test_the_rebuild_does_not_duplicate_what_build_already_added():
    """Idempotence across the two calls.

    ``build_non_metabolite_reactions`` skips any reaction id already in the
    model it is handed, so when the first call DOES admit something (a genome
    whose species map is pre-populated, i.e. the RAST-role path) the second call
    must not re-add it -- cobra raises on duplicate reaction ids, so a
    regression here would surface as an exception rather than a bad model.
    """
    pytest.importorskip("cobra")
    import cobra

    builder = _SpontaneousBuilder()
    # Pre-seed the map, as build_metabolic_reactions would on the RAST path.
    base_model = cobra.Model(id_or_model="g1", name="Toy organism")
    builder.base_model = base_model
    metabolite = _mint_model_metabolite("cpd00007_c")
    builder.template_species_to_model_species["cpd00007_c"] = metabolite
    base_model.add_metabolites([metabolite])

    fake_self = _SpontaneousSelf(builder, {"cpd00007_c"})
    _out, mdlutl = MSReconstructionUtils.build_metabolic_model(
        fake_self,
        genome=_OrderedGenome(),
        genome_classifier=None,
        gs_template="gn",
        gs_template_obj=object(),
        core_template=object(),
        base_model=base_model,
        atp_safe=False,
        reactions_to_add={"rxn00001": ["g1"]},
    )

    ids = [rxn.id for rxn in mdlutl.model.reactions]
    assert ids.count("rxn05468_c0") == 1, f"duplicate diffusion reaction in {ids}"
    # The first call admitted it; the second found it already present.
    assert builder.admitted_per_call[0] == 1
    assert builder.admitted_per_call[-1] == 0
