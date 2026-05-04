"""Pydantic validation tests for notebook engine schema models."""

from datetime import datetime, timezone

import pytest

from kbutillib.notebook.schema.entity import EntityKind, EntityRef
from kbutillib.notebook.schema.experiment import (
    Computation,
    Experiment,
    ExternalDataset,
    Sample,
)
from kbutillib.notebook.schema.media import Media
from kbutillib.notebook.schema.strain import Mutation, Strain
from kbutillib.notebook.schema.vector import Vector, VectorType
from kbutillib.notebook.session import NotebookSession


# ------------------------------------------------------------------
# EntityKind / EntityRef
# ------------------------------------------------------------------


class TestEntityKind:
    def test_valid_values(self):
        assert EntityKind("gene") == EntityKind.GENE
        assert EntityKind("reaction") == EntityKind.REACTION
        assert EntityKind("metabolite") == EntityKind.METABOLITE

    def test_invalid_value(self):
        with pytest.raises(ValueError):
            EntityKind("invalid")


class TestEntityRef:
    def test_valid(self):
        ref = EntityRef(kind=EntityKind.GENE, id="b0001", namespace="ecoli_genome")
        assert ref.kind == EntityKind.GENE
        assert ref.id == "b0001"
        assert ref.namespace == "ecoli_genome"

    def test_missing_field_raises(self):
        with pytest.raises(Exception):
            EntityRef(kind=EntityKind.GENE, id="b0001")  # missing namespace


# ------------------------------------------------------------------
# Media
# ------------------------------------------------------------------


class TestMedia:
    def test_default_source(self):
        m = Media(id="glucose_minimal")
        assert m.source == "kbase"
        assert m.inline_composition is None

    def test_inline_source(self):
        m = Media(id="custom", source="inline", inline_composition={"cpd00001": 10.0})
        assert m.inline_composition == {"cpd00001": 10.0}

    def test_invalid_source(self):
        with pytest.raises(Exception):
            Media(id="x", source="invalid")

    def test_kbase_source_no_composition_is_valid(self):
        """Media with source='kbase' and no inline_composition is valid at construction."""
        m = Media(id="glucose", source="kbase")
        assert m.inline_composition is None

    def test_msmedia_source_no_composition_is_valid(self):
        """Media with source='msmedia' and no inline_composition is valid at construction."""
        m = Media(id="rich_media", source="msmedia")
        assert m.inline_composition is None

    def test_resolve_composition_inline(self, tmp_session):
        """resolve_composition returns inline_composition directly for inline source."""
        m = Media(id="custom", source="inline", inline_composition={"cpd00001": 10.0, "cpd00002": 5.0})
        comp = m.resolve_composition(tmp_session)
        assert comp == {"cpd00001": 10.0, "cpd00002": 5.0}

    def test_resolve_composition_inline_missing_raises(self, tmp_session):
        """resolve_composition raises ValueError for inline source with no composition."""
        m = Media(id="bad", source="inline")
        with pytest.raises(ValueError, match="no inline_composition"):
            m.resolve_composition(tmp_session)

    def test_resolve_composition_kbase_stub(self, tmp_session):
        """resolve_composition raises NotImplementedError for kbase source."""
        m = Media(id="glucose", source="kbase")
        with pytest.raises(NotImplementedError, match="KBase media lookup"):
            m.resolve_composition(tmp_session)

    def test_resolve_composition_msmedia_stub(self, tmp_session):
        """resolve_composition raises NotImplementedError for msmedia source."""
        m = Media(id="rich", source="msmedia")
        with pytest.raises(NotImplementedError, match="ModelSEED media lookup"):
            m.resolve_composition(tmp_session)

    def test_kbase_media_sample_registration(self, tmp_session):
        """A Sample with kbase media and no composition can be registered successfully."""
        sample = Sample(
            id="kbase_sample",
            media=Media(id="glucose", source="kbase"),
            strains={"wt": 1.0},
        )
        exp = tmp_session.experiments.register_sample(sample)
        assert exp.id == "kbase_sample"
        # Round-trip: retrieve and check media is preserved
        loaded = tmp_session.experiments.get("kbase_sample")
        assert loaded.payload.media.source == "kbase"
        assert loaded.payload.media.inline_composition is None


# ------------------------------------------------------------------
# Sample
# ------------------------------------------------------------------


class TestSample:
    def test_valid(self):
        s = Sample(
            id="s1",
            media=Media(id="glucose"),
            strains={"wt": 1.0},
        )
        assert s.id == "s1"

    def test_strains_must_sum_to_one(self):
        with pytest.raises(ValueError, match="sum to 1.0"):
            Sample(
                id="s1",
                media=Media(id="glucose"),
                strains={"wt": 0.5, "ko": 0.3},
            )

    def test_strains_empty_raises(self):
        with pytest.raises(ValueError, match="must not be empty"):
            Sample(
                id="s1",
                media=Media(id="glucose"),
                strains={},
            )

    def test_sample_abundance_must_be_positive(self):
        with pytest.raises(ValueError, match="must be > 0"):
            Sample(
                id="s1",
                media=Media(id="glucose"),
                strains={"wt": 0.0, "ko": 1.0},
            )
        with pytest.raises(ValueError, match="must be > 0"):
            Sample(
                id="s1",
                media=Media(id="glucose"),
                strains={"wt": -0.5, "ko": 1.5},
            )


# ------------------------------------------------------------------
# Computation
# ------------------------------------------------------------------


class TestComputation:
    def test_valid(self):
        c = Computation(
            id="c1",
            model_ref="iML1515",
            media=Media(id="glucose"),
        )
        assert c.parameters == {}

    def test_with_parameters(self):
        c = Computation(
            id="c1",
            model_ref="iML1515",
            media=Media(id="glucose"),
            parameters={"objective": "biomass"},
        )
        assert c.parameters["objective"] == "biomass"


# ------------------------------------------------------------------
# ExternalDataset
# ------------------------------------------------------------------


class TestExternalDataset:
    def test_valid_sources(self):
        for src in ("literature", "public_db", "collaborator", "other"):
            ext = ExternalDataset(id="e1", source=src)
            assert ext.source == src

    def test_invalid_source(self):
        with pytest.raises(Exception):
            ExternalDataset(id="e1", source="invalid")


# ------------------------------------------------------------------
# Experiment
# ------------------------------------------------------------------


class TestExperiment:
    def test_payload_kind_mismatch(self):
        sample = Sample(id="s1", media=Media(id="glucose"), strains={"wt": 1.0})
        with pytest.raises(ValueError, match="payload"):
            Experiment(
                id="s1",
                kind="computation",
                payload=sample,
                created_at=datetime.now(timezone.utc),
            )

    def test_valid_sample_experiment(self):
        sample = Sample(id="s1", media=Media(id="glucose"), strains={"wt": 1.0})
        exp = Experiment(
            id="s1",
            kind="sample",
            payload=sample,
            created_at=datetime.now(timezone.utc),
        )
        assert exp.kind == "sample"


# ------------------------------------------------------------------
# Mutation / Strain
# ------------------------------------------------------------------


class TestMutation:
    def test_valid(self):
        m = Mutation(
            kind="knockout",
            target=EntityRef(kind=EntityKind.GENE, id="b0001", namespace="ecoli"),
        )
        assert m.kind == "knockout"

    def test_invalid_kind(self):
        with pytest.raises(Exception):
            Mutation(
                kind="invalid",
                target=EntityRef(kind=EntityKind.GENE, id="b0001", namespace="ecoli"),
            )

    def test_mutation_target_must_be_gene(self):
        with pytest.raises(ValueError, match="Mutation.target must be a GENE"):
            Mutation(
                kind="knockout",
                target=EntityRef(kind=EntityKind.REACTION, id="rxn0001", namespace="model"),
            )


class TestStrain:
    def test_valid(self):
        s = Strain(id="wt", parent_genome="ecoli_genome")
        assert s.mutations == []

    def test_with_mutations(self):
        s = Strain(
            id="ko1",
            parent_genome="ecoli_genome",
            mutations=[
                Mutation(
                    kind="knockout",
                    target=EntityRef(kind=EntityKind.GENE, id="b0001", namespace="ecoli"),
                )
            ],
        )
        assert len(s.mutations) == 1


# ------------------------------------------------------------------
# VectorType
# ------------------------------------------------------------------


class TestVectorType:
    def test_valid(self):
        vt = VectorType(domain="transcriptomics", scale="log2")
        assert vt.domain == "transcriptomics"

    def test_invalid_domain(self):
        with pytest.raises(ValueError, match="Unknown domain"):
            VectorType(domain="invalid_domain", scale="log2")

    def test_invalid_scale(self):
        with pytest.raises(ValueError, match="Unknown scale"):
            VectorType(domain="transcriptomics", scale="invalid_scale")

    def test_invalid_projection(self):
        with pytest.raises(ValueError, match="Unknown projection"):
            VectorType(domain="transcriptomics", scale="log2", projection="invalid")

    def test_valid_projection(self):
        vt = VectorType(domain="transcriptomics", scale="log2", projection="gpr_max")
        assert vt.projection == "gpr_max"


# ------------------------------------------------------------------
# Vector
# ------------------------------------------------------------------


class TestVector:
    def test_valid(self):
        v = Vector(
            id="v1",
            type=VectorType(domain="transcriptomics", scale="log2"),
            experiment_id="exp1",
            entity_kind=EntityKind.GENE,
            entity_namespace="ecoli",
            columns=["rep1", "rep2"],
            parquet_path="vectors/v1.parquet",
            content_hash="abc123",
            created_at=datetime.now(timezone.utc),
        )
        assert v.parents == []
        assert v.derivation is None
