"""Unit tests for the native role-based build half of the reconstruction layer.

Everything here runs on FAKES.  The real-dependency counterpart lives in
``tests/modeling/test_recon_build_real_dependency.py``; a real reconstruction
takes 3-6 minutes, so anything a fake can decide is decided here.

What is covered:

* ``MSBuilder.build()`` is NEVER called, and the draft comes from
  ``build_base_model`` with ``annotate_with_rast=False`` and the two opt-in
  flags;
* classification is SKIPPED when the caller supplies the template, and goes
  through ``classify_genome`` when it does not;
* the two roleless-path compensations are gated on ``reactions_to_add``;
* ATP correction runs exactly once, and the upstream
  ``load_default_medias``-is-a-bool trap is worked around by pre-loading;
* :class:`ReconstructionResult` is frozen, enumerates its fields, and reports
  revisions read from distribution metadata rather than synthesised;
* every call into ModelSEEDpy from the module uses keyword arguments only
  (the AST test).
"""

from __future__ import annotations

import ast
import dataclasses
import json
import pathlib
from typing import Any

import pytest

recon = pytest.importorskip("kbutillib.domains.modeling.ms_reconstruction_utils")
MSReconstructionUtils = recon.MSReconstructionUtils
ReconstructionResult = recon.ReconstructionResult

MODULE_PATH = pathlib.Path(recon.__file__)


# ---------------------------------------------------------------------------
# Fakes
#
# These are deliberately NOT the ``_Ordered*`` fakes in
# ``test_ms_reconstruction_utils.py``: those were written for the roleless
# gene-mapping path and record call order.  These record ARGUMENTS, and their
# ``build_base_model`` carries the pinned ModelSEEDpy's real parameter names so
# the signature probe in ``_base_model_keywords`` sees the shape it will see in
# production.
# ---------------------------------------------------------------------------


class _FakeModel:
    def __init__(self, *, reaction_ids=(), gene_ids=(), metabolite_count=0):
        self.reactions = [_FakeReaction(rid) for rid in reaction_ids]
        self.genes = [object() for _ in gene_ids]
        self.metabolites = [object() for _ in range(metabolite_count)]
        self.objective = _FakeObjective()

    @property
    def exchanges(self):
        return [r for r in self.reactions if r.id.startswith("EX_")]

    def add_reactions(self, reactions):
        self.reactions.extend(reactions)

    def remove_reactions(self, ids):
        self.reactions = [r for r in self.reactions if r.id not in set(ids)]


class _FakeReaction:
    def __init__(self, rid):
        self.id = rid


class _FakeObjective:
    expression = "1.0*bio1 - 1.0*bio1_reverse_b18f7"


class _FakeMdlUtl:
    def __init__(self, model):
        self.model = model
        self._attributes: dict = {}
        self.wsid = None
        self.recon_builder = None
        self.atp_tests = None

    def get_attributes(self):
        return self._attributes

    def nonexchange_reaction_count(self):
        return len([r for r in self.model.reactions if not r.id.startswith("EX_")])

    def is_core(self, rxn):
        return True


class _FakeBuilder:
    """Stands in for both the ``MSBuilder`` class and its instances."""

    #: Reactions the draft comes out with.  Includes exchanges, a biomass and a
    #: drain so the draft facts have something to measure.
    DRAFT_REACTION_IDS = (
        "rxn00001_c0",
        "rxn00002_c0",
        "bio1",
        "EX_cpd00007_e0",
        "EX_cpd00011_e0",
        "DM_cpd00012_c0",
        "SK_cpd00013_c0",
    )

    def __init__(self):
        self.construction_kwargs: list[dict] = []
        self.base_model_kwargs: list[dict] = []
        self.non_metabolite_calls = 0
        self.exchange_calls = 0
        self.build_calls = 0

    def __call__(self, *, genome, template, ontology_term="RAST", **kwargs):
        self.construction_kwargs.append(
            dict(kwargs, genome=genome, template=template, ontology_term=ontology_term)
        )
        return self

    def build(self, *args, **kwargs):  # pragma: no cover - must never be reached
        self.build_calls += 1
        raise AssertionError(
            "MSBuilder.build() must never be called: upstream runs ATP "
            "correction AND gap-fill inside it and returns a 4-tuple"
        )

    def build_base_model(
        self,
        model_or_id,
        index="0",
        allow_all_non_grp_reactions=False,
        annotate_with_rast=True,
        biomass_classic=False,
        biomass_gc=0.5,
        add_reaction_from_rast_annotation=True,
        *,
        match_role_aliases=False,
        allow_incomplete_complexes=True,
    ):
        self.base_model_kwargs.append(
            {
                "model_or_id": model_or_id,
                "index": index,
                "allow_all_non_grp_reactions": allow_all_non_grp_reactions,
                "annotate_with_rast": annotate_with_rast,
                "match_role_aliases": match_role_aliases,
                "allow_incomplete_complexes": allow_incomplete_complexes,
            }
        )
        return _FakeModel(
            reaction_ids=self.DRAFT_REACTION_IDS,
            gene_ids=("g1", "g2", "g3"),
            metabolite_count=5,
        )

    def build_non_metabolite_reactions(
        self, cobra_model, allow_all_non_grp_reactions=False
    ):
        self.non_metabolite_calls += 1
        return []

    def add_exchanges_to_model(self, model, extra_cell="e0"):
        self.exchange_calls += 1
        return []


class _FakeTemplate:
    def __init__(self, template_id, name):
        self.id = template_id
        self.name = name


class _FakeATPCorrection:
    """Removes one reaction, so the removed set is non-trivial."""

    REMOVES = "rxn00002_c0"

    def __init__(self):
        self.construction_kwargs: list[dict] = []
        self.runs = 0
        self.cumulative_core_gapfilling = ["rxn09999_c0"]
        self.selected_media = [_FakeMedia("Glc.O2"), _FakeMedia("empty")]
        self.media_gapfill_stats = {_FakeMedia("Glc.O2"): {"new": {}, "reversed": {}}}
        self.atp_hydrolysis = _FakeReaction("rxn00062_c0")
        self.tests = [{"objective": "rxn00062_c0", "threshold": 1.0}]
        self._model = None

    def __call__(self, **kwargs):
        self.construction_kwargs.append(kwargs)
        self._model = kwargs["model_or_mdlutl"].model
        return self

    def run_atp_correction(self):
        self.runs += 1
        self._model.remove_reactions([self.REMOVES])
        return self.tests


class _FakeMedia:
    def __init__(self, media_id):
        self.id = media_id

    def __hash__(self):
        return hash(self.id)

    def __eq__(self, other):
        return isinstance(other, _FakeMedia) and other.id == self.id


class _FakeGenome:
    id = "g1"
    scientific_name = "Toy organism"
    features: list = []


class _FakeSelf:
    """Fake ``MSReconstructionUtils`` carrying only what the build path uses."""

    def __init__(self, *, builder=None, atp=None, classify_to=None):
        self.MSBuilder = builder if builder is not None else _FakeBuilder()
        self.MSATPCorrection = atp if atp is not None else _FakeATPCorrection()
        self.MSModelUtil = self
        self.modelseedpy_data_dir = "/nonexistent"
        self.templates = {"core": "core_ref", "gn": "gn_ref", "gp": "gp_ref"}
        self.classify_calls: list[dict] = []
        self._classify_to = classify_to
        self._mdlutl = None
        self.warnings: list[str] = []

    def get(self, mdl):
        self._mdlutl = _FakeMdlUtl(mdl)
        return self._mdlutl

    def get_template(self, ref, _none):
        return _FakeTemplate(f"template_from_{ref}", f"Template from {ref}")

    def classify_genome(self, **kwargs):
        self.classify_calls.append(kwargs)
        if self._classify_to is None:
            raise AssertionError("classify_genome should not have been called")
        return self._classify_to

    def log_warning(self, message):
        self.warnings.append(message)

    def log_info(self, message):
        pass

    def _add_reactions_from_gene_mapping(self, *args, **kwargs):
        return []

    # Borrowed from the real class: the signature probe and the compensation
    # gate are part of what is under test.
    _base_model_keywords = MSReconstructionUtils._base_model_keywords
    _apply_roleless_path_compensations = (
        MSReconstructionUtils._apply_roleless_path_compensations
    )


def _build(fake_self: _FakeSelf, **overrides: Any):
    """Call the real ``build_metabolic_model`` against a fake ``self``."""
    kwargs = {
        "genome": _FakeGenome(),
        "genome_classifier": None,
        "gs_template": "gn",
        "gs_template_obj": _FakeTemplate("template_gram_neg", "Gram negative"),
        "core_template": _FakeTemplate("template_core", "Core"),
        "atp_safe": True,
        "load_default_medias": False,
        "return_reconstruction_result": True,
    }
    kwargs.update(overrides)
    return MSReconstructionUtils.build_metabolic_model(fake_self, **kwargs)


# ---------------------------------------------------------------------------
# The draft comes from build_base_model, never from build()
# ---------------------------------------------------------------------------


class TestDraftIsBuiltWithBuildBaseModel:
    def test_build_is_never_called(self):
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder))

        assert builder.build_calls == 0
        assert len(builder.base_model_kwargs) == 1

    def test_rast_annotation_is_switched_off(self):
        """Left at ModelSEEDpy's default True, ``MSBuilder`` constructs a
        ``RastClient`` and makes a live network call that discards the caller's
        annotations -- and that a KBase CTS job cannot make at all."""
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder))

        assert builder.base_model_kwargs[0]["annotate_with_rast"] is False

    def test_allow_all_non_grp_reactions_stays_false(self):
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder))

        assert builder.base_model_kwargs[0]["allow_all_non_grp_reactions"] is False

    @pytest.mark.parametrize("match_role_aliases", [True, False])
    @pytest.mark.parametrize("allow_incomplete_complexes", [True, False])
    def test_the_two_opt_in_flags_are_forwarded(
        self, match_role_aliases, allow_incomplete_complexes
    ):
        builder = _FakeBuilder()
        _build(
            _FakeSelf(builder=builder),
            match_role_aliases=match_role_aliases,
            allow_incomplete_complexes=allow_incomplete_complexes,
        )

        passed = builder.base_model_kwargs[0]
        assert passed["match_role_aliases"] is match_role_aliases
        assert passed["allow_incomplete_complexes"] is allow_incomplete_complexes

    def test_flag_defaults_match_the_opt_in_contract(self):
        """Alias matching OFF, incomplete complexes ON -- upstream's defaults.

        Alias matching is opt-in because native ModelSEED templates alias
        nearly every role with an identifier such as ``msfr.1407``.
        """
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder))

        passed = builder.base_model_kwargs[0]
        assert passed["match_role_aliases"] is False
        assert passed["allow_incomplete_complexes"] is True

    def test_the_ontology_term_reaches_the_builder(self):
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder), ontology_term="KBDL_FOLDED")

        assert builder.construction_kwargs[0]["ontology_term"] == "KBDL_FOLDED"

    def test_the_default_ontology_term_is_rast(self):
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder))

        assert builder.construction_kwargs[0]["ontology_term"] == "RAST"
        assert recon.DEFAULT_ONTOLOGY_TERM == "RAST"

    def test_the_supplied_template_reaches_the_builder(self):
        builder = _FakeBuilder()
        template = _FakeTemplate("kbdl_template", "KBDL template")
        _build(_FakeSelf(builder=builder), gs_template_obj=template)

        assert builder.construction_kwargs[0]["template"] is template


class TestUnsupportedKeywords:
    """A ModelSEEDpy without the alias change must not silently drop a request."""

    class _OldBuilder(_FakeBuilder):
        def build_base_model(
            self,
            model_or_id,
            index="0",
            allow_all_non_grp_reactions=False,
            annotate_with_rast=True,
        ):
            self.base_model_kwargs.append({"model_or_id": model_or_id})
            return _FakeModel(
                reaction_ids=_FakeBuilder.DRAFT_REACTION_IDS, gene_ids=("g1",)
            )

    def test_requesting_alias_matching_raises(self):
        with pytest.raises(recon.ModelSEEDpyCapabilityError, match="match_role_aliases"):
            _build(_FakeSelf(builder=self._OldBuilder()), match_role_aliases=True)

    def test_requesting_complete_complexes_only_raises(self):
        with pytest.raises(
            recon.ModelSEEDpyCapabilityError, match="allow_incomplete_complexes"
        ):
            _build(
                _FakeSelf(builder=self._OldBuilder()),
                allow_incomplete_complexes=False,
            )

    def test_defaults_do_not_raise_on_an_older_modelseedpy(self):
        """Neither flag at its upstream default means nothing was requested, so
        there is nothing to refuse."""
        builder = self._OldBuilder()
        _out, mdlutl, _result = _build(_FakeSelf(builder=builder))

        assert mdlutl is not None
        assert "match_role_aliases" not in builder.base_model_kwargs[0]


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassificationIsSkippedWhenTheTemplateIsSupplied:
    def test_supplied_template_with_auto_skips_classification(self):
        """``gs_template="auto"`` plus a supplied template is not ambiguous: the
        caller already chose. KBDL builds its MSTemplate from a kbdl-template/1
        document and no classifier verdict could have selected it."""
        fake_self = _FakeSelf(classify_to=None)  # classify_genome would raise
        _out, mdlutl, result = _build(fake_self, gs_template="auto")

        assert fake_self.classify_calls == []
        assert mdlutl is not None
        assert result.genome_class is None

    def test_classification_runs_when_no_template_is_supplied(self):
        fake_self = _FakeSelf(classify_to="gp")
        _out, _mdlutl, result = _build(
            fake_self, gs_template="auto", gs_template_obj=None
        )

        assert len(fake_self.classify_calls) == 1
        assert result.genome_class == "Gram Positive"
        # The key it returned is what the template lookup used.
        assert result.template_id == "template_from_gp_ref"

    def test_the_ontology_term_reaches_the_classifier(self):
        fake_self = _FakeSelf(classify_to="gn")
        _build(
            fake_self,
            gs_template="auto",
            gs_template_obj=None,
            ontology_term="KBDL_FOLDED",
        )

        assert fake_self.classify_calls[0]["ontology_term"] == "KBDL_FOLDED"

    def test_an_explicit_template_key_never_classifies(self):
        fake_self = _FakeSelf(classify_to=None)
        _build(fake_self, gs_template="gn", gs_template_obj=None)

        assert fake_self.classify_calls == []


# ---------------------------------------------------------------------------
# The roleless-path compensations are gated
# ---------------------------------------------------------------------------


class TestCompensationsAreGatedOnReactionsToAdd:
    """MEASURED no-ops on a role-based draft.

    ``agent-io/work-records/task-d1302dc4.md`` section (f): on a 957-reaction
    *H. influenzae* draft built against the native gram-negative template,
    re-running ``build_non_metabolite_reactions`` returned 0 reactions and
    ``add_exchanges_to_model`` added none.  The draft already carried 73
    exchanges and all 19 of the template's spontaneous/universal reactions.
    """

    def test_no_compensation_on_the_native_path(self):
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder), reactions_to_add=None)

        assert builder.non_metabolite_calls == 0
        assert builder.exchange_calls == 0

    def test_an_empty_mapping_counts_as_not_supplied(self):
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder), reactions_to_add={})

        assert builder.non_metabolite_calls == 0
        assert builder.exchange_calls == 0

    def test_both_compensations_still_run_for_existing_callers(self):
        builder = _FakeBuilder()
        _build(_FakeSelf(builder=builder), reactions_to_add={"rxn00001": ["g1"]})

        assert builder.non_metabolite_calls == 1
        assert builder.exchange_calls == 1

    def test_the_result_records_which_path_was_taken(self):
        _out, _mdlutl, native = _build(_FakeSelf(), reactions_to_add=None)
        _out, _mdlutl, injected = _build(
            _FakeSelf(), reactions_to_add={"rxn00001": ["g1"]}
        )

        assert native.reactions_to_add_supplied is False
        assert injected.reactions_to_add_supplied is True


# ---------------------------------------------------------------------------
# ATP correction
# ---------------------------------------------------------------------------


class TestATPCorrection:
    def test_runs_exactly_once(self):
        atp = _FakeATPCorrection()
        _build(_FakeSelf(atp=atp))

        assert atp.runs == 1

    def test_is_given_the_supplied_core_template(self):
        atp = _FakeATPCorrection()
        core = _FakeTemplate("kbdl_core", "KBDL core")
        _build(_FakeSelf(atp=atp), core_template=core)

        assert atp.construction_kwargs[0]["core_template"] is core

    def test_does_not_run_when_atp_safe_is_false(self):
        atp = _FakeATPCorrection()
        _out, _mdlutl, result = _build(_FakeSelf(atp=atp), atp_safe=False)

        assert atp.runs == 0
        assert result.post_atp_correction_ran is False
        assert result.post_atp_correction_invocations == 0
        assert result.post_atp_reaction_count is None
        assert result.core_template_id is None

    def test_the_tests_are_published_on_the_model_util(self):
        atp = _FakeATPCorrection()
        _out, mdlutl, _result = _build(_FakeSelf(atp=atp))

        assert mdlutl.atp_tests == atp.tests

    def test_the_removed_set_is_reported(self):
        atp = _FakeATPCorrection()
        _out, _mdlutl, result = _build(_FakeSelf(atp=atp))

        assert result.post_atp_removed_reaction_ids == (_FakeATPCorrection.REMOVES,)
        assert result.post_atp_reaction_count == (
            result.draft_reaction_count - 1
        )

    def test_media_gapfill_stats_are_keyed_by_id_not_by_object(self):
        """``MSATPCorrection`` keys them by the ``MSMedia`` OBJECT, which makes
        ``json.dumps`` raise "keys must be str, int, float, bool or None"."""
        _out, _mdlutl, result = _build(_FakeSelf())

        stats = result.post_atp_analysis["media_gapfill_stats"]
        assert set(stats) == {"Glc.O2"}
        json.dumps(result.to_dict())


class TestDefaultATPMediaPreload:
    """Upstream's ``load_default_medias`` trap, worked around by pre-loading.

    Inside ``MSATPCorrection.__init__`` the name ``load_default_medias`` resolves
    to the BOOL PARAMETER, not to the module-level function of the same name, so
    ``load_default_medias(default_media_path)`` raises ``TypeError: 'bool'
    object is not callable`` for any truthy value.
    """

    def test_media_are_preloaded_and_the_flag_is_forced_false(self, monkeypatch):
        from modelseedpy.core import msatpcorrection

        loaded = [[_FakeMedia("Glc.O2"), 0.01], [_FakeMedia("Ac.O2"), 0.01]]
        calls: list[dict] = []

        def fake_loader(*, default_media_path):
            calls.append({"default_media_path": default_media_path})
            return list(loaded)

        monkeypatch.setattr(msatpcorrection, "load_default_medias", fake_loader)

        atp = _FakeATPCorrection()
        _build(_FakeSelf(atp=atp), load_default_medias=True)

        assert len(calls) == 1
        assert calls[0]["default_media_path"] == "/nonexistent/atp_medias.tsv"
        passed = atp.construction_kwargs[0]
        assert passed["load_default_medias"] is False
        assert passed["atp_medias"] == loaded

    def test_callers_media_are_appended_after_the_defaults(self, monkeypatch):
        from modelseedpy.core import msatpcorrection

        monkeypatch.setattr(
            msatpcorrection,
            "load_default_medias",
            lambda *, default_media_path: [[_FakeMedia("Glc.O2"), 0.01]],
        )

        atp = _FakeATPCorrection()
        mine = _FakeMedia("MyMedium")
        _build(_FakeSelf(atp=atp), load_default_medias=True, atp_medias=[mine])

        passed = atp.construction_kwargs[0]["atp_medias"]
        assert passed[-1] is mine
        assert len(passed) == 2

    def test_the_flag_is_forwarded_when_the_module_has_no_loader(self, monkeypatch):
        """The FORK has no module-level function; its loader is the METHOD
        ``self.load_default_medias``, which ``__init__`` calls unambiguously and
        which works.  There the flag is forwarded as the caller asked."""
        from modelseedpy.core import msatpcorrection

        monkeypatch.delattr(msatpcorrection, "load_default_medias", raising=True)

        atp = _FakeATPCorrection()
        _build(_FakeSelf(atp=atp), load_default_medias=True)

        assert atp.construction_kwargs[0]["load_default_medias"] is True
        assert atp.construction_kwargs[0]["atp_medias"] == []

    def test_nothing_is_preloaded_when_the_caller_opts_out(self, monkeypatch):
        from modelseedpy.core import msatpcorrection

        calls: list = []
        monkeypatch.setattr(
            msatpcorrection,
            "load_default_medias",
            lambda **kwargs: calls.append(kwargs) or [],
        )

        atp = _FakeATPCorrection()
        _build(_FakeSelf(atp=atp), load_default_medias=False)

        assert calls == []
        assert atp.construction_kwargs[0]["load_default_medias"] is False


# ---------------------------------------------------------------------------
# ReconstructionResult
# ---------------------------------------------------------------------------


class TestReconstructionResultShape:
    def test_it_is_a_frozen_dataclass(self):
        assert dataclasses.is_dataclass(ReconstructionResult)
        assert ReconstructionResult.__dataclass_params__.frozen is True

    def test_fields_cannot_be_reassigned(self):
        _out, _mdlutl, result = _build(_FakeSelf())

        with pytest.raises(dataclasses.FrozenInstanceError):
            result.draft_reaction_count = 0

    def test_every_field_is_explicitly_enumerated_and_untyped_none_free(self):
        """No ``**kwargs``, no catch-all dict: a reader of the class sees every
        fact the layer promises."""
        names = [f.name for f in dataclasses.fields(ReconstructionResult)]
        assert len(names) == len(set(names))
        for required in (
            "draft_reaction_count",
            "draft_gene_count",
            "draft_exchange_count",
            "draft_objective_expression",
            "post_atp_analysis",
            "post_atp_removed_reaction_ids",
            "post_gapfill_results",
            "template_id",
            "core_template_id",
            "modelseedpy_version",
            "modelseedpy_revision",
            "kbutillib_version",
            "kbutillib_revision",
        ):
            assert required in names

    def test_every_stage_field_docstring_names_its_stage(self):
        """A count without its stage is unreadable: "957 reactions" means three
        different things at the three boundaries."""
        source = ast.parse(MODULE_PATH.read_text())
        class_node = next(
            node
            for node in ast.walk(source)
            if isinstance(node, ast.ClassDef) and node.name == "ReconstructionResult"
        )

        docstrings: dict[str, str] = {}
        pending = None
        for statement in class_node.body:
            if isinstance(statement, ast.AnnAssign) and isinstance(
                statement.target, ast.Name
            ):
                pending = statement.target.id
            elif (
                pending is not None
                and isinstance(statement, ast.Expr)
                and isinstance(statement.value, ast.Constant)
                and isinstance(statement.value.value, str)
            ):
                docstrings[pending] = statement.value.value
                pending = None

        stage_words = ("DRAFT", "POST-ATP", "POST-GAP-FILL", "stage")
        missing = []
        for field in dataclasses.fields(ReconstructionResult):
            text = docstrings.get(field.name)
            if text is None:
                missing.append(f"{field.name}: no docstring")
            elif not any(word in text for word in stage_words):
                missing.append(f"{field.name}: docstring names no stage")
        assert not missing, missing


class TestReconstructionResultContent:
    def test_draft_facts_are_measured_at_the_draft_boundary(self):
        """DRAFT = fully assembled, uncorrected: the exchanges, biomass and
        objective are in, the ATP-correction removal is NOT."""
        _out, _mdlutl, result = _build(_FakeSelf())

        assert result.draft_reaction_count == len(_FakeBuilder.DRAFT_REACTION_IDS)
        assert result.draft_gene_count == 3
        assert result.draft_metabolite_count == 5
        assert result.draft_exchange_count == 2
        assert result.draft_drain_count == 2
        assert result.draft_biomass_ids == ("bio1",)
        assert result.draft_objective_expression == _FakeObjective.expression
        # ATPM is added by MSBuilder.build(), which this layer never calls, and
        # by MSATPCorrection, which runs after the draft boundary.
        assert result.draft_atpm_present is False

    def test_the_gapfill_field_is_an_empty_tuple_from_build(self):
        """Build stops at POST-ATP by design; ``gapfill_metabolic_model`` is the
        stage that fills this."""
        _out, _mdlutl, result = _build(_FakeSelf())

        assert result.post_gapfill_results == ()

    def test_template_and_core_template_identities_are_recorded(self):
        gs = _FakeTemplate("kbdl_template", "KBDL gram-negative")
        core = _FakeTemplate("kbdl_core", "KBDL core")
        _out, _mdlutl, result = _build(
            _FakeSelf(), gs_template_obj=gs, core_template=core
        )

        assert result.template_id == "kbdl_template"
        assert result.template_name == "KBDL gram-negative"
        assert result.core_template_id == "kbdl_core"
        assert result.core_template_name == "KBDL core"

    def test_the_genome_id_is_recorded(self):
        _out, _mdlutl, result = _build(_FakeSelf())

        assert result.genome_id == "g1"

    def test_to_dict_is_json_serialisable(self):
        _out, _mdlutl, result = _build(_FakeSelf())

        round_tripped = json.loads(json.dumps(result.to_dict()))
        assert round_tripped["draft_biomass_ids"] == ["bio1"]


class TestLegacyReturnPath:
    def test_the_default_is_still_the_two_value_pair(self):
        """Every existing caller unpacks two values -- the CLI at
        ``interfaces/cli/model.py`` and ``kb_build_metabolic_models``."""
        returned = _build(_FakeSelf(), return_reconstruction_result=False)

        assert len(returned) == 2
        current_output, mdlutl = returned
        assert isinstance(current_output, dict)
        assert mdlutl is not None

    def test_opting_in_appends_a_third_value(self):
        returned = _build(_FakeSelf(), return_reconstruction_result=True)

        assert len(returned) == 3
        assert isinstance(returned[2], ReconstructionResult)

    def test_the_legacy_output_dict_is_unchanged_by_opting_in(self):
        pair = _build(_FakeSelf(), return_reconstruction_result=False)
        triple = _build(_FakeSelf(), return_reconstruction_result=True)

        assert pair[0] == triple[0]


# ---------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------


class TestDistributionProvenance:
    def test_an_absent_distribution_reports_unknown_for_both(self):
        version, revision = recon.resolve_distribution_provenance(
            "no-such-distribution-90001"
        )

        assert version == recon.UNKNOWN_REVISION == "unknown"
        assert revision == recon.UNKNOWN_REVISION

    def test_the_revision_is_the_direct_url_commit(self, monkeypatch):
        class _Dist:
            version = "0.4.3"

            def read_text(self, name):
                assert name == "direct_url.json"
                return json.dumps(
                    {
                        "url": "https://github.com/cshenry/ModelSEEDpy.git",
                        "vcs_info": {"vcs": "git", "commit_id": "e1155a41fa53b9"},
                    }
                )

        import importlib.metadata

        monkeypatch.setattr(
            importlib.metadata, "distribution", lambda name: _Dist()
        )

        version, revision = recon.resolve_distribution_provenance("modelseedpy")
        assert version == "0.4.3"
        assert revision == "e1155a41fa53b9"

    def test_a_wheel_without_direct_url_reports_unknown_not_a_guess(self):
        class _Dist:
            version = "0.4.2"

            def read_text(self, name):
                return None

        import importlib.metadata

        import pytest as _pytest

        with _pytest.MonkeyPatch.context() as patch:
            patch.setattr(importlib.metadata, "distribution", lambda name: _Dist())
            version, revision = recon.resolve_distribution_provenance("modelseedpy")

        assert version == "0.4.2"
        assert revision == "unknown"

    def test_an_editable_install_reports_unknown(self, monkeypatch):
        """``pip install -e .`` records ``dir_info.editable`` and no commit.
        Nothing is inferred from the git checkout the source sits in: the
        working tree is not evidence about the code that was imported."""

        class _Dist:
            version = "0.1.0"

            def read_text(self, name):
                return json.dumps(
                    {"url": "file:///repo", "dir_info": {"editable": True}}
                )

        import importlib.metadata

        monkeypatch.setattr(
            importlib.metadata, "distribution", lambda name: _Dist()
        )

        _version, revision = recon.resolve_distribution_provenance("KBUtilLib")
        assert revision == "unknown"

    def test_malformed_direct_url_json_reports_unknown_rather_than_raising(
        self, monkeypatch
    ):
        class _Dist:
            version = "0.4.3"

            def read_text(self, name):
                return "{not json"

        import importlib.metadata

        monkeypatch.setattr(
            importlib.metadata, "distribution", lambda name: _Dist()
        )

        _version, revision = recon.resolve_distribution_provenance("modelseedpy")
        assert revision == "unknown"

    def test_the_result_carries_both_distributions(self):
        _out, _mdlutl, result = _build(_FakeSelf())

        for value in (
            result.modelseedpy_version,
            result.modelseedpy_revision,
            result.kbutillib_version,
            result.kbutillib_revision,
        ):
            assert isinstance(value, str) and value


# ---------------------------------------------------------------------------
# Every call into ModelSEEDpy uses keyword arguments only
#
# Fork and upstream disagree about the MEANING of the second positional
# parameter of MSBuilder.build: upstream binds it to ``gapfill_media``, the fork
# to ``index``.  The old ``builder.build(base_model, "0", False, False)`` bound
# "0" to gapfill_media, False to index, and left ``annotate_with_rast`` at its
# default True -- i.e. a live RAST network call -- which is how a positional call
# into this API fails: silently and wrongly, not with a TypeError.
# ---------------------------------------------------------------------------

MODELSEEDPY_CALL_TARGETS = (
    "MSBuilder",
    "MSGapfill",
    "MSATPCorrection",
    "MSTemplateBuilder",
)


def _attribute_chain(func: ast.AST):
    """Split a callee into ``(root_name, attributes_root_outward)``."""
    attributes = []
    cursor = func
    while isinstance(cursor, ast.Attribute):
        attributes.append(cursor.attr)
        cursor = cursor.value
    root = cursor.id if isinstance(cursor, ast.Name) else None
    return root, list(reversed(attributes))


def _target_aliases(tree: ast.AST) -> dict:
    """Local names bound to a ModelSEEDpy object, e.g. ``builder``.

    ``builder = self.MSBuilder(genome=..., template=...)`` makes every later
    ``builder.<method>(...)`` a call into ModelSEEDpy, so the probe has to
    follow the binding or it misses the ``build_base_model`` call that is the
    entire point of this task.
    """
    aliases = {}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Call)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
        ):
            root, attributes = _attribute_chain(node.value.func)
            hit = ({root} | set(attributes)) & set(MODELSEEDPY_CALL_TARGETS)
            if hit:
                aliases[node.targets[0].id] = sorted(hit)[0]
    return aliases


def _modelseedpy_calls():
    """Every call made ON a ModelSEEDpy class or instance in the module.

    Returns:
        A list of ``(lineno, target_class, rendered_callee, positional_count)``.

    A call with ``**kwargs`` reports zero positional arguments and so passes.
    That is the shape ``build_base_model`` is called with on purpose: WHICH
    keywords the installed ModelSEEDpy accepts is decided at runtime by
    ``_base_model_keywords``, and a ``**`` call is still keyword-only.

    Only calls whose receiver IS the target class or an instance of it count --
    at most one attribute past the target.  ``builder.base_model.add_metabolites``
    and ``atpcorrection.media_gapfill_stats.items`` are calls on a cobra model
    and on a dict that happen to be reachable through a ModelSEEDpy object; the
    fork/upstream signature divergence this rule exists for is at the
    ModelSEEDpy API boundary, not past it.
    """
    tree = ast.parse(MODULE_PATH.read_text())
    aliases = _target_aliases(tree)
    targets = set(MODELSEEDPY_CALL_TARGETS)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        root, attributes = _attribute_chain(node.func)
        if root in targets:
            target, tail = root, attributes
        elif set(attributes) & targets:
            index = next(i for i, a in enumerate(attributes) if a in targets)
            target, tail = attributes[index], attributes[index + 1 :]
        elif root in aliases:
            target, tail = aliases[root], attributes
        else:
            continue
        if len(tail) > 1:
            continue
        found.append((node.lineno, target, ast.unparse(node.func), len(node.args)))
    return found


def _offenders(calls):
    return [
        f"{MODULE_PATH.name}:{lineno} {callee}(...) passes {count} positional "
        f"argument(s)"
        for lineno, _target, callee, count in calls
        if count
    ]


def test_the_ast_probe_actually_finds_the_calls():
    """Guards the test itself: a probe that matches nothing passes vacuously."""
    calls = _modelseedpy_calls()

    assert len(calls) >= 6, calls
    callees = {callee for _l, _t, callee, _c in calls}
    assert any("build_base_model" in callee for callee in callees)
    assert any("run_atp_correction" in callee for callee in callees)
    assert any("add_exchanges_to_model" in callee for callee in callees)
    assert any("build_non_metabolite_reactions" in callee for callee in callees)
    found_targets = {target for _l, target, _c, _n in calls}
    assert {"MSBuilder", "MSATPCorrection", "MSGapfill"} <= found_targets

    # And it must NOT reach past the ModelSEEDpy boundary.
    assert not any("add_metabolites" in callee for callee in callees)
    assert not any(callee.endswith(".items") for callee in callees)


def test_non_gapfill_modelseedpy_calls_use_keyword_arguments_only():
    """MSBuilder, MSATPCorrection and MSTemplateBuilder, all call sites.

    The old ``builder.build(base_model, "0", False, False)`` is why this test
    exists: upstream binds the second positional to ``gapfill_media`` and the
    fork to ``index``, so that call bound ``"0"`` to ``gapfill_media``, ``False``
    to ``index``, and left ``annotate_with_rast`` at its default True -- a live
    RAST network call.  A positional call into this API fails silently and
    wrongly, not with a ``TypeError``.
    """
    calls = [call for call in _modelseedpy_calls() if call[1] != "MSGapfill"]

    assert not _offenders(calls), _offenders(calls)


@pytest.mark.xfail(strict=True, reason="part C")
def test_msgapfill_calls_use_keyword_arguments_only():
    """``MSGapfill(mdlutl, templates, source_models, all_tests, ...)`` is called
    with four positional arguments in both ``gapfill_metabolic_model`` and
    ``run_comprehensive_gapfill_on_model``, and ``run_multi_gapfill`` /
    ``compute_reaction_weights_from_expression_data`` pass theirs positionally
    too.  All of that is part C's (kbu-recon-gapfill) to fix; when it does, this
    xfail will strict-fail and must be deleted.
    """
    calls = [call for call in _modelseedpy_calls() if call[1] == "MSGapfill"]

    assert not _offenders(calls), _offenders(calls)
